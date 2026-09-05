"""Process-local Telethon asyncio runtime (Wave 7A).

Cached ``TelegramClient`` instances in ``ClientManager`` are bound to the event
loop they were created/connected on. Flask/Gunicorn sync workers historically
called ``asyncio.new_event_loop()`` per request, which breaks the second Telethon
call in the same worker (dialogs → history).

Ownership model (Option A):
  one daemon thread + one long-lived event loop per Gunicorn process
  sync callers submit coroutines via ``run_coroutine_threadsafe``

Each Gunicorn worker process gets its own runtime and client cache — intentional.
"""
from __future__ import annotations

import atexit
import asyncio
import threading
from concurrent.futures import Future
from typing import Any, Coroutine, Optional, TypeVar

import structlog

logger = structlog.get_logger(__name__)

T = TypeVar("T")

_DEFAULT_TIMEOUT_SEC = 120.0

_lock = threading.Lock()
_loop: Optional[asyncio.AbstractEventLoop] = None
_thread: Optional[threading.Thread] = None
_started = False
_stopping = False


def _thread_main(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    try:
        loop.run_forever()
    finally:
        try:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass


def ensure_started() -> asyncio.AbstractEventLoop:
    """Start the persistent Telethon loop thread if needed. Thread-safe."""
    global _loop, _thread, _started
    with _lock:
        if _started and _loop is not None and _loop.is_running():
            return _loop
        if _stopping:
            raise RuntimeError("telethon_runtime_stopping")
        loop = asyncio.new_event_loop()
        thread = threading.Thread(
            target=_thread_main,
            args=(loop,),
            name="telethon-runtime",
            daemon=True,
        )
        thread.start()
        # Wait until the loop is actually running
        ready = threading.Event()

        def _mark_ready() -> None:
            ready.set()

        loop.call_soon_threadsafe(_mark_ready)
        if not ready.wait(timeout=5.0):
            raise RuntimeError("telethon_runtime_start_timeout")
        _loop = loop
        _thread = thread
        _started = True
        logger.info("telethon_runtime_started", thread=thread.name)
        return loop


def run(coro: Coroutine[Any, Any, T], *, timeout: float = _DEFAULT_TIMEOUT_SEC) -> T:
    """
    Run a coroutine on the process-local Telethon event loop.

    Safe to call from Flask/Gunicorn sync worker threads.
    """
    if asyncio.iscoroutine(coro) is False:
        raise TypeError("telethon_runtime.run expects a coroutine")
    loop = ensure_started()
    if threading.current_thread() is _thread:
        # Already on the Telethon thread — run directly
        return loop.run_until_complete(coro)

    future: Future = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return future.result(timeout=timeout)
    except Exception:
        # Ensure the coroutine is cancelled if the waiter times out / fails
        future.cancel()
        raise


def is_running() -> bool:
    return bool(_started and _loop is not None and _loop.is_running() and not _stopping)


async def _disconnect_clients() -> None:
    try:
        from src.clients.manager import client_manager

        await client_manager.disconnect_all()
    except Exception as e:
        logger.warning("telethon_runtime_disconnect_failed", error=str(e))


def shutdown(*, timeout: float = 15.0) -> None:
    """Disconnect Telethon clients and stop the persistent loop (idempotent)."""
    global _loop, _thread, _started, _stopping
    with _lock:
        if not _started or _loop is None:
            _stopping = False
            return
        _stopping = True
        loop = _loop
        thread = _thread

    try:
        fut = asyncio.run_coroutine_threadsafe(_disconnect_clients(), loop)
        try:
            fut.result(timeout=min(10.0, timeout))
        except Exception as e:
            logger.warning("telethon_runtime_shutdown_disconnect_error", error=str(e))
    except Exception as e:
        logger.warning("telethon_runtime_shutdown_submit_failed", error=str(e))

    try:
        loop.call_soon_threadsafe(loop.stop)
    except Exception:
        pass
    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)

    with _lock:
        _loop = None
        _thread = None
        _started = False
        _stopping = False
        logger.info("telethon_runtime_stopped")


def _atexit_shutdown() -> None:
    try:
        shutdown(timeout=5.0)
    except Exception:
        pass


atexit.register(_atexit_shutdown)
