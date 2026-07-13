"""
Dedicated readiness worker process (systemd).

Runs the canonical background loop as a single long-lived process to avoid per-worker
duplication inside Gunicorn and reduce Telegram SQLite session lock contention.

Expected usage:
  READINESS_WORKER_ENABLED=true /opt/autostory/venv/bin/python scripts/run_readiness_worker.py
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import threading

# Ensure repo root is importable when launched via absolute path.
_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.abspath(os.path.join(_here, ".."))
if _root not in sys.path:
    sys.path.insert(0, _root)

import structlog  # noqa: E402

logger = structlog.get_logger(__name__)


def main() -> int:
    enabled_raw = os.environ.get("READINESS_WORKER_ENABLED", "true").strip().lower()
    enabled = enabled_raw in ("1", "true", "yes", "on")
    if not enabled:
        logger.error(
            "readiness_worker_standalone_refusing_disabled",
            READINESS_WORKER_ENABLED=enabled_raw,
            hint="Set READINESS_WORKER_ENABLED=true for the dedicated systemd unit only.",
        )
        return 2

    try:
        from src.core.database import init_db

        init_db()
    except Exception as e:
        logger.error("readiness_worker_init_db_failed", error=str(e))
        return 1

    from src.clients.readiness_worker import readiness_worker_loop  # noqa: E402

    stop = threading.Event()

    def _handle_stop(*_args):
        stop.set()

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    logger.info("readiness_worker_standalone_starting", pid=os.getpid())
    try:
        asyncio.run(readiness_worker_loop(stop_event=stop))
    except Exception as e:
        logger.error("readiness_worker_standalone_failed", error=str(e))
        return 1

    logger.info("readiness_worker_standalone_stopped", pid=os.getpid())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
