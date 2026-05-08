"""
Send pacing guardrails (production safety).

Derives last-send instants from ``message_deliveries`` (SENT only).
Configurable via environment variables (seconds, integers unless noted).
"""
from __future__ import annotations

import os
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func

from src.core.datetime_utc import to_utc_iso_z


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return int(default)
    try:
        return max(0, int(str(raw).strip(), 10))
    except ValueError:
        return int(default)


def load_send_pacing_config() -> Dict[str, int]:
    return {
        "min_account_interval_sec": _env_int("SEND_MIN_ACCOUNT_INTERVAL_SEC", 60),
        "min_account_target_interval_sec": _env_int("SEND_MIN_ACCOUNT_TARGET_INTERVAL_SEC", 600),
        "post_join_cooldown_sec": _env_int("SEND_POST_JOIN_COOLDOWN_SEC", 30),
        "jitter_min_sec": _env_int("SEND_RANDOM_JITTER_MIN_SEC", 20),
        "jitter_max_sec": _env_int("SEND_RANDOM_JITTER_MAX_SEC", 120),
        "test_min_account_interval_sec": _env_int("SEND_TEST_MIN_ACCOUNT_INTERVAL_SEC", 20),
    }


def _utc_naive_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _last_sent_instant(db: Any, account_id: int, target_id: Optional[int] = None) -> Optional[datetime]:
    """Latest SENT delivery instant for account (optionally scoped to one target)."""
    from src.core.scheduler_models import MessageDelivery

    _st = func.lower(func.trim(MessageDelivery.status))
    q = db.query(func.max(func.coalesce(MessageDelivery.sent_at, MessageDelivery.created_at))).filter(
        MessageDelivery.account_id == int(account_id),
        _st == "sent",
    )
    if target_id is not None:
        q = q.filter(MessageDelivery.target_id == int(target_id))
    row = q.scalar()
    return row if isinstance(row, datetime) else None


def _pair_sent_count(db: Any, account_id: int, target_id: int) -> int:
    from src.core.scheduler_models import MessageDelivery

    _st = func.lower(func.trim(MessageDelivery.status))
    return int(
        db.query(MessageDelivery)
        .filter(
            MessageDelivery.account_id == int(account_id),
            MessageDelivery.target_id == int(target_id),
            _st == "sent",
        )
        .count()
    )


def get_send_pacing_decision(
    db: Any,
    account_id: int,
    target_id: int,
    *,
    is_test: bool = False,
    binding_created_at: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Returns:
      allowed, retry_after_sec, reasons, next_allowed_at (ISO Z),
      next_allowed_at_dt (naive UTC datetime or None).
    """
    cfg = load_send_pacing_config()
    now_naive = now or _utc_naive_now()
    reasons: List[str] = []

    from src.clients.readiness_store import (
        STAT_NOT_AUTH,
        fetch_snapshot,
        snapshot_row_valid,
    )

    snap = fetch_snapshot(db, int(account_id))
    if snap and snapshot_row_valid(snap, now_naive) and snap.status == STAT_NOT_AUTH:
        # Hard block — operator must re-login.
        retry_after = max(300, cfg["min_account_interval_sec"])
        until = now_naive + timedelta(seconds=retry_after)
        return {
            "allowed": False,
            "retry_after_sec": retry_after,
            "reasons": ["Account readiness: NOT_AUTHORIZED — re-login required before sends"],
            "next_allowed_at": to_utc_iso_z(until),
            "next_allowed_at_dt": until,
        }

    # TEMP_CONNECT is surfaced as WATCH in reputation — do not hard-block sends here
    # (Telethon connect may still fail; operator sees readiness RETRY).

    last_any = _last_sent_instant(db, account_id, None)
    last_pair = _last_sent_instant(db, account_id, target_id)

    min_acc = cfg["test_min_account_interval_sec"] if is_test else cfg["min_account_interval_sec"]
    min_pair = cfg["min_account_target_interval_sec"]

    candidates: List[datetime] = [now_naive]

    if last_any:
        candidates.append(last_any + timedelta(seconds=min_acc))
    if last_pair:
        candidates.append(last_pair + timedelta(seconds=min_pair))

    # First successful send to this pair: enforce post-join / binding-age floor.
    if _pair_sent_count(db, account_id, target_id) == 0 and binding_created_at:
        bc = binding_created_at
        if getattr(bc, "tzinfo", None):
            bc = bc.replace(tzinfo=None)  # treat as UTC wall if aware
        candidates.append(bc + timedelta(seconds=cfg["post_join_cooldown_sec"]))

    base = max(candidates)

    if not is_test:
        j0, j1 = cfg["jitter_min_sec"], cfg["jitter_max_sec"]
        if j1 < j0:
            j0, j1 = j1, j0
        base = base + timedelta(seconds=random.randint(j0, j1))

    if base <= now_naive:
        return {
            "allowed": True,
            "retry_after_sec": 0,
            "reasons": [],
            "next_allowed_at": to_utc_iso_z(now_naive),
            "next_allowed_at_dt": now_naive,
        }

    delta = base - now_naive
    retry_after = max(1, int(delta.total_seconds()) + 1)
    return {
        "allowed": False,
        "retry_after_sec": retry_after,
        "reasons": [
            f"Pacing: next send not before {to_utc_iso_z(base)} (UTC)",
        ],
        "next_allowed_at": to_utc_iso_z(base),
        "next_allowed_at_dt": base,
    }


def defer_scheduled_job_for_pacing(_db: Any, job_id: int, next_allowed_at_naive: datetime) -> None:
    """Push ``run_at`` forward; reset ``RUNNING`` + lease back to ``PENDING``. Uses ``last_error='pacing_deferred'``.

    Uses a dedicated transaction with SQLite lock retries. The ``db`` session argument
    is kept for call-site compatibility; the update is applied via ``get_db_context()``.
    """
    import structlog

    from src.core.database import get_db_context, run_with_sqlite_lock_retry
    from src.core.scheduler_models import JobStatus, ScheduledJob

    pacing_logger = structlog.get_logger(__name__)

    def _do() -> None:
        with get_db_context() as db2:
            job = db2.query(ScheduledJob).filter(ScheduledJob.id == int(job_id)).first()
            if not job:
                return
            prev_lo = job.lease_owner
            prev_lu = job.lease_until
            was_running = str(job.status) == JobStatus.RUNNING.value
            cur = job.run_at or next_allowed_at_naive
            job.run_at = max(cur, next_allowed_at_naive)
            job.last_error = "pacing_deferred"
            job.updated_at = _utc_naive_now()
            job.status = JobStatus.PENDING.value
            job.lease_until = None
            job.lease_owner = None
            if was_running and (prev_lo is not None or prev_lu is not None):
                pacing_logger.info(
                    "scheduler_job_lease_released",
                    job_id=int(job_id),
                    lease_owner=prev_lo,
                    lease_until=prev_lu,
                    reason="pacing_deferred",
                )

    run_with_sqlite_lock_retry(
        _do,
        operation="defer_scheduled_job_for_pacing",
        job_id=int(job_id),
    )


def maybe_schedule_or_block_job(
    db: Any,
    job_id: int,
    account_id: int,
    target_id: int,
    *,
    is_test: bool = False,
    binding_created_at: Optional[datetime] = None,
) -> Tuple[bool, Dict[str, Any]]:
    """
    If pacing blocks a scheduled (non-test) job, defer ``run_at`` and return (False, decision).

    For send tests, callers should check *before* creating a job — this helper is
    primarily used from ``execute_job`` for worker-driven jobs.
    """
    dec = get_send_pacing_decision(
        db,
        account_id,
        target_id,
        is_test=is_test,
        binding_created_at=binding_created_at,
    )
    if dec.get("allowed"):
        return True, dec
    if not is_test and dec.get("next_allowed_at_dt"):
        defer_scheduled_job_for_pacing(db, job_id, dec["next_allowed_at_dt"])
    return False, dec
