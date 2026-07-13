"""
Background Telegram readiness resolver.

Continuously refreshes ``account_readiness_snapshots`` out-of-band so dashboard
readiness does not stay stuck on CHECKING. Connects to Telegram for auth probes
only — never publishes stories or runs scheduler/campaign actions.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from typing import Optional

import structlog

from src.ai_agent.account_allowlist import RESERVED_AI_AGENT_ACCOUNT_IDS
from src.clients import readiness_store
from src.clients.manager import client_manager
from src.clients.readiness_worker_policy import (
    ReadinessWorkerConfig,
    select_probe_candidates,
    write_runtime_status,
)
from src.clients.session_resolve import human_message_for_code, probe_telethon_session_kind
from src.core.account_runtime_state import is_account_active
from src.core.database import get_db_context
from src.core.models import Account

logger = structlog.get_logger(__name__)

_PROBE_IN_FLIGHT: set[int] = set()
_PROBE_SLOT_LOCK = asyncio.Lock()

_LOCK_FAILURE_CODES = frozenset({"session_lock_timeout", "session_db_locked"})
_TEMP_CONNECT_FAIL_REASONS = frozenset({
    "session_db_locked",
    "session_lock_timeout",
    "failed_connect_network",
})


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _acquire_probe_slot(
    account_id: int,
    *,
    wait: bool = False,
    wait_timeout_sec: float = 30.0,
) -> bool:
    """Per-account probe slot: prevents overlapping worker/manual deep checks."""
    deadline = time.monotonic() + wait_timeout_sec if wait else time.monotonic()
    while True:
        async with _PROBE_SLOT_LOCK:
            if int(account_id) not in _PROBE_IN_FLIGHT:
                _PROBE_IN_FLIGHT.add(int(account_id))
                return True
        if not wait or time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.2)


async def _release_probe_slot(account_id: int) -> None:
    async with _PROBE_SLOT_LOCK:
        _PROBE_IN_FLIGHT.discard(int(account_id))


def probe_slot_in_flight(account_id: int) -> bool:
    return int(account_id) in _PROBE_IN_FLIGHT


def _task_exception_phase(exc: BaseException) -> str:
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "per_account_timeout"
    if isinstance(exc, asyncio.CancelledError):
        return "cancelled"
    if exc.__class__.__name__ in ("ExceptionGroup", "BaseExceptionGroup"):
        return "exception_group"
    return "deep_check"


async def _deep_check_one(
    account_id: int,
    *,
    wait_for_slot: bool = False,
    dry_run: bool = False,
) -> Optional[str]:
    """
    Deep readiness check for one account; persists v1 snapshot when appropriate.
    Returns a short outcome token for cycle metrics (never raises).
    """
    aid = int(account_id)
    phase = "load_account_db"
    t0 = time.perf_counter()

    if not await _acquire_probe_slot(aid, wait=wait_for_slot):
        logger.info(
            "account_probe_skipped",
            account_id=aid,
            reason_code="probe_in_flight",
            duration_ms=0,
        )
        return "skipped_in_flight"

    logger.info("account_probe_started", account_id=aid, dry_run=dry_run)

    if dry_run:
        await _release_probe_slot(aid)
        logger.info(
            "account_probe_completed",
            account_id=aid,
            result="dry_run",
            reason_code="dry_run",
            duration_ms=int((time.perf_counter() - t0) * 1000),
        )
        return "dry_run"

    try:
        if is_account_active(aid):
            logger.info("readiness_worker_skip_active", account_id=aid, reason="active_runtime")
            return "skipped_active"

        with get_db_context() as db:
            acc = db.query(Account).filter(Account.id == aid).first()
            if acc is None:
                logger.info("readiness_worker_skip", account_id=aid, reason="missing_account")
                return "missing_account"

            kind, err = probe_telethon_session_kind(acc)
            session_exists = err is None and kind in ("file", "string")
            if err is not None:
                phase = "resolver_error"
                with get_db_context() as wdb:
                    readiness_store.mark_account_readiness_error(
                        wdb,
                        aid,
                        human_message_for_code(err) or str(err),
                        failure_code=str(err),
                    )
                logger.info(
                    "readiness_session_material_error",
                    account_id=aid,
                    failure_code=err,
                    phase=phase,
                    outcome="session_error",
                )
                return "session_error"

            if not session_exists:
                logger.info("readiness_worker_skip", account_id=aid, reason="no_session_material")
                return "skipped_no_session"

        if is_account_active(aid):
            logger.info("readiness_worker_skip_active", account_id=aid, reason="active_before_connect")
            return "skipped_active"

        phase = "connect_account"
        wrapper = None
        fail_reason: Optional[str] = None
        cfg = ReadinessWorkerConfig.from_environ()
        try:
            wrapper, fail_reason = await client_manager.connect_account(
                aid,
                session_lock_timeout_sec=cfg.session_lock_acquire_sec,
            )

            if wrapper is not None:
                phase = "mark_ready"
                with get_db_context() as db:
                    readiness_store.mark_account_ready_after_success(db, aid, "readiness_worker_ok")
                logger.info(
                    "account_probe_completed",
                    account_id=aid,
                    result="ready",
                    reason_code="ready",
                    duration_ms=int((time.perf_counter() - t0) * 1000),
                    phase=phase,
                )
                return "ready"

            if fail_reason in ("unauthorized_session",):
                phase = "mark_not_authorized"
                with get_db_context() as db:
                    readiness_store.mark_account_not_authorized(
                        db, aid, "readiness_worker_not_authorized"
                    )
                logger.warning(
                    "account_probe_completed",
                    account_id=aid,
                    result="not_authorized",
                    reason_code="not_authorized",
                    duration_ms=int((time.perf_counter() - t0) * 1000),
                    phase=phase,
                )
                return "not_authorized"

            if fail_reason in _LOCK_FAILURE_CODES:
                phase = "mark_temp_connect_lock"
                with get_db_context() as db:
                    if readiness_store.should_preserve_ready_on_session_lock_contention(
                        db, aid, fail_reason
                    ):
                        logger.info(
                            "readiness_worker_preserve_ready_on_lock",
                            account_id=aid,
                            failure_code=fail_reason,
                        )
                        return "skipped_locked"
                    readiness_store.mark_account_temp_connect(
                        db,
                        aid,
                        readiness_store.human_msg_temp(fail_reason),
                        failure_code=fail_reason,
                    )
                logger.warning(
                    "account_probe_failed",
                    account_id=aid,
                    result="skipped_locked",
                    reason_code=fail_reason,
                    duration_ms=int((time.perf_counter() - t0) * 1000),
                    phase=phase,
                )
                return "skipped_locked"

            if fail_reason in _TEMP_CONNECT_FAIL_REASONS:
                phase = "mark_temp_connect_network"
                with get_db_context() as db:
                    readiness_store.mark_account_temp_connect(
                        db,
                        aid,
                        readiness_store.human_msg_temp(fail_reason),
                        failure_code=fail_reason,
                    )
                logger.warning(
                    "account_probe_failed",
                    account_id=aid,
                    result="temp_error",
                    reason_code=fail_reason,
                    duration_ms=int((time.perf_counter() - t0) * 1000),
                    phase=phase,
                )
                return "temp_error"

            phase = "mark_readiness_error"
            reason_txt = human_message_for_code(fail_reason) if fail_reason else None
            if not reason_txt:
                reason_txt = str(fail_reason or "connect_failed")
            with get_db_context() as db:
                readiness_store.mark_account_readiness_error(
                    db,
                    aid,
                    reason_txt,
                    failure_code=str(fail_reason or "connect_failed"),
                )
            logger.warning(
                "account_probe_failed",
                account_id=aid,
                result="session_error",
                reason_code=fail_reason,
                duration_ms=int((time.perf_counter() - t0) * 1000),
                phase=phase,
            )
            return "session_error"

        except Exception as exc:
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            logger.warning(
                "account_probe_failed",
                account_id=aid,
                result="failed_exception",
                reason_code="deep_check_exception",
                duration_ms=int((time.perf_counter() - t0) * 1000),
                phase=phase,
                exception_type=type(exc).__name__,
                error=repr(exc),
                traceback=tb,
            )
            try:
                with get_db_context() as db:
                    readiness_store.mark_account_readiness_error(
                        db,
                        aid,
                        f"readiness_worker_exception: {exc}",
                        failure_code="deep_check_exception",
                    )
            except Exception as mark_exc:
                logger.error(
                    "readiness_worker_mark_error_failed",
                    account_id=aid,
                    error=str(mark_exc),
                )
            return "failed_exception"
        finally:
            try:
                await client_manager.remove_account(aid)
            except Exception as rm_exc:
                logger.warning(
                    "readiness_worker_remove_account_failed",
                    account_id=aid,
                    error=str(rm_exc),
                )
    finally:
        await _release_probe_slot(aid)


async def deep_check_one_for_operator(account_id: int) -> Optional[str]:
    """Manual operator refresh — waits briefly for background probe slot."""
    return await _deep_check_one(int(account_id), wait_for_slot=True)


async def _interruptible_sleep(seconds: float, stop_event: Optional[threading.Event]) -> None:
    remaining = max(0.0, float(seconds))
    while remaining > 0:
        if stop_event is not None and stop_event.is_set():
            return
        step = min(1.0, remaining)
        await asyncio.sleep(step)
        remaining -= step


async def readiness_worker_loop(
    stop_event: Optional[threading.Event] = None,
) -> None:
    cfg = ReadinessWorkerConfig.from_environ()
    validation_errors = cfg.validate()
    if validation_errors:
        logger.error("worker_config_invalid", errors=validation_errors)
        raise RuntimeError("; ".join(validation_errors))

    run_once = (os.environ.get("READINESS_WORKER_RUN_ONCE", "").strip().lower() in (
        "1", "true", "yes", "on"
    ))

    logger.info(
        "worker_started",
        pid=os.getpid(),
        max_parallel=cfg.max_parallel,
        per_account_timeout_sec=cfg.per_account_timeout_sec,
        session_lock_acquire_sec=cfg.session_lock_acquire_sec,
        ready_ttl_sec=readiness_store.READY_TTL_SEC,
        trust_window_sec=readiness_store.READINESS_TRUST_WINDOW_SEC,
        temp_ttl_sec=readiness_store.TEMP_TTL_SEC,
        cycle_sleep_sec=cfg.cycle_sleep_sec,
        account_delay_sec=cfg.account_delay_sec,
        batch_size=cfg.batch_size,
        auth_failure_backoff_sec=cfg.auth_failure_backoff_sec,
        transient_backoff_sec=cfg.transient_backoff_sec,
        dry_run=cfg.dry_run,
        reserved_ai_account_ids=sorted(RESERVED_AI_AGENT_ACCOUNT_IDS),
        allow_ids=sorted(cfg.allow_ids) if cfg.allow_ids else None,
        run_once=run_once,
    )

    sem = asyncio.Semaphore(cfg.max_parallel)
    last_error: Optional[str] = None

    async def _run_one(aid: int) -> Optional[str]:
        async with sem:
            try:
                return await asyncio.wait_for(
                    _deep_check_one(aid, dry_run=cfg.dry_run),
                    timeout=cfg.per_account_timeout_sec,
                )
            except Exception as exc:
                logger.warning(
                    "account_probe_failed",
                    account_id=aid,
                    result="failed_timeout",
                    reason_code=_task_exception_phase(exc),
                    exception_type=type(exc).__name__,
                    error=repr(exc),
                )
                return "failed_timeout"

    while True:
        if stop_event is not None and stop_event.is_set():
            logger.info("worker_stopping", pid=os.getpid())
            break

        cycle_id = str(uuid.uuid4())
        cycle_t0 = time.perf_counter()
        now = _utc_now_naive()
        logger.info("cycle_started", cycle_id=cycle_id)

        ready_n = 0
        not_authorized_n = 0
        skipped_locked = 0
        temp_errors = 0
        session_errors = 0
        failed_other = 0
        skipped_in_flight = 0
        candidate_ids: list[int] = []
        skip_counts: dict[str, int] = {}

        try:
            with get_db_context() as db:
                accounts = db.query(Account).order_by(Account.id).all()
                candidate_ids, skip_counts = select_probe_candidates(db, accounts, config=cfg, now=now)

            tasks: list[asyncio.Task] = []
            for aid in candidate_ids:
                if stop_event is not None and stop_event.is_set():
                    break
                tasks.append(asyncio.create_task(_run_one(aid)))
                await asyncio.sleep(cfg.account_delay_sec)

            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for i, r in enumerate(results):
                    aid = candidate_ids[i] if i < len(candidate_ids) else -1
                    if r is None:
                        continue
                    if isinstance(r, str):
                        if r == "ready":
                            ready_n += 1
                        elif r == "not_authorized":
                            not_authorized_n += 1
                        elif r == "skipped_locked":
                            skipped_locked += 1
                        elif r == "temp_error":
                            temp_errors += 1
                        elif r == "session_error":
                            session_errors += 1
                        elif r == "skipped_in_flight":
                            skipped_in_flight += 1
                        elif r in ("failed_exception", "failed_timeout"):
                            failed_other += 1
                        elif r == "dry_run":
                            pass
                        else:
                            failed_other += 1
                    elif isinstance(r, BaseException):
                        failed_other += 1
                        logger.warning(
                            "account_probe_failed",
                            account_id=aid,
                            result="task_exception",
                            reason_code=_task_exception_phase(r),
                            exception_type=type(r).__name__,
                            error=repr(r),
                        )

        except Exception as e:
            last_error = str(e)
            logger.warning("readiness_worker_cycle_error", cycle_id=cycle_id, error=last_error)

        cycle_duration_sec = round(time.perf_counter() - cycle_t0, 3)
        checked = len(candidate_ids)
        failed = temp_errors + session_errors + failed_other

        status_payload = {
            "updated_at": now.isoformat(),
            "cycle_id": cycle_id,
            "cycle_duration_sec": cycle_duration_sec,
            "checked": checked,
            "ready": ready_n,
            "not_authorized": not_authorized_n,
            "skipped_locked": skipped_locked,
            "skipped_in_flight": skipped_in_flight,
            "temp_errors": temp_errors,
            "session_errors": session_errors,
            "failed_other": failed_other,
            "skip_counts": skip_counts,
            "next_cycle_sleep_sec": cfg.cycle_sleep_sec,
            "dry_run": cfg.dry_run,
            "allow_ids": sorted(cfg.allow_ids) if cfg.allow_ids else None,
            "last_error": last_error,
            "pid": os.getpid(),
        }
        write_runtime_status(status_payload)

        logger.info(
            "cycle_completed",
            cycle_id=cycle_id,
            checked=checked,
            ready=ready_n,
            not_authorized=not_authorized_n,
            skipped_locked=skipped_locked,
            skipped_in_flight=skipped_in_flight,
            skip_counts=skip_counts,
            temp_errors=temp_errors,
            session_errors=session_errors,
            failed_other=failed_other,
            failed=failed,
            cycle_duration_sec=cycle_duration_sec,
            next_cycle_sleep_sec=float(cfg.cycle_sleep_sec),
        )

        if run_once:
            logger.info("readiness_worker_run_once_complete", pid=os.getpid())
            break

        await _interruptible_sleep(cfg.cycle_sleep_sec, stop_event)

    logger.info("worker_stopped", pid=os.getpid())


_started = False
_lock = threading.Lock()


def start_readiness_worker_background() -> None:
    """Start the worker in a daemon thread (one per process)."""
    global _started
    enabled_raw = os.environ.get("READINESS_WORKER_ENABLED", "true").strip().lower()
    enabled = enabled_raw in ("1", "true", "yes", "on")
    if not enabled:
        logger.info("readiness_worker_skipped_disabled", enabled=enabled_raw, pid=os.getpid())
        return

    with _lock:
        if _started:
            return
        _started = True

    logger.info("readiness_worker_background_start", pid=os.getpid())

    def _thread_main() -> None:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.create_task(readiness_worker_loop())
            loop.run_forever()
        except Exception as e:
            logger.error("readiness_worker_thread_failed", error=str(e))

    t = threading.Thread(target=_thread_main, name="readiness-worker", daemon=True)
    t.start()
