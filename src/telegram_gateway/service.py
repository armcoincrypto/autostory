"""Enqueue gateway jobs (sync, for Flask / Gunicorn workers)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any, Collection, Optional

from sqlalchemy import or_

from src.core.database import get_db_context, run_with_sqlite_lock_retry
from src.telegram_gateway.models import TelegramGatewayJob


def _running_stale_sec() -> int:
    # Default 10 minutes: worker crash / SIGKILL mid-job recovery without stealing active work.
    raw = os.environ.get("TELEGRAM_GATEWAY_RUNNING_STALE_SEC", "600").strip()
    try:
        return max(30, min(int(raw), 7200))
    except ValueError:
        return 600


def enqueue_job(
    *,
    account_id: int,
    task_type: str,
    target: str,
    payload: Optional[dict[str, Any]] = None,
    run_after: Optional[datetime] = None,
) -> int:
    """Insert a pending job and return its id."""

    def _do() -> int:
        now = datetime.utcnow()
        with get_db_context() as db:
            row = TelegramGatewayJob(
                account_id=int(account_id),
                task_type=str(task_type),
                target=(target or "")[:512],
                payload_json=payload or {},
                status="pending",
                attempts=0,
                run_after=run_after or now,
                created_at=now,
                updated_at=now,
            )
            db.add(row)
            db.flush()
            db.refresh(row)
            return int(row.id)

    return run_with_sqlite_lock_retry(
        _do,
        operation="telegram_gateway_enqueue_job",
        max_attempts=int(os.environ.get("SQLITE_WRITE_MAX_RETRIES", "8")),
    )


def get_job(job_id: int) -> Optional[TelegramGatewayJob]:
    with get_db_context() as db:
        return db.get(TelegramGatewayJob, int(job_id))


def claim_next_jobs(
    db,
    *,
    limit: int = 10,
    allowed_account_ids: Optional[Collection[int]] = None,
) -> list[TelegramGatewayJob]:
    """
    Mark up to ``limit`` jobs as running. At most one running job per account_id.
    Intended for a single gateway worker process (SQLite-friendly).

    If ``allowed_account_ids`` is set, only jobs whose ``account_id`` is in the
    collection are claimed (others stay pending/retry for ops cleanup).
    """
    now = datetime.utcnow()
    candidates = (
        db.query(TelegramGatewayJob)
        .filter(
            TelegramGatewayJob.status.in_(("pending", "retry")),
            or_(
                TelegramGatewayJob.run_after.is_(None),
                TelegramGatewayJob.run_after <= now,
            ),
        )
        .order_by(TelegramGatewayJob.id.desc())
        .limit(limit * 8)
        .all()
    )
    try:
        from src.ai_agent.account_allowlist import (
            ai_agent_allowlist_configured,
            resolve_ai_agent_allowed_account_ids,
        )

        if ai_agent_allowlist_configured():
            pri = resolve_ai_agent_allowed_account_ids(db)
            if pri:
                candidates = sorted(
                    candidates,
                    key=lambda j: (0 if int(j.account_id) in pri else 1, -int(j.id)),
                )
    except Exception:
        pass
    claimed: list[TelegramGatewayJob] = []
    busy_accounts: set[int] = set(
        int(r[0])
        for r in db.query(TelegramGatewayJob.account_id)
        .filter(TelegramGatewayJob.status == "running")
        .distinct()
        .all()
    )
    allowed: Optional[set[int]] = None
    if allowed_account_ids is not None:
        allowed = {int(x) for x in allowed_account_ids}

    for job in candidates:
        if len(claimed) >= limit:
            break
        aid = int(job.account_id)
        if allowed is not None and aid not in allowed:
            continue
        if aid in busy_accounts:
            continue
        fresh = (
            db.query(TelegramGatewayJob)
            .filter(
                TelegramGatewayJob.id == job.id,
                TelegramGatewayJob.status.in_(("pending", "retry")),
            )
            .first()
        )
        if not fresh:
            continue
        fresh.status = "running"
        fresh.updated_at = datetime.utcnow()
        db.flush()
        busy_accounts.add(aid)
        claimed.append(fresh)
    return claimed


def mark_job_done(db, job_id: int, result: dict[str, Any]) -> None:
    row = db.get(TelegramGatewayJob, int(job_id))
    if not row:
        return
    row.status = "done"
    row.result_json = result
    row.error_code = None
    row.error_message = None
    row.updated_at = datetime.utcnow()
    db.flush()


def mark_job_failed(
    db,
    job_id: int,
    *,
    error_code: str,
    error_message: str,
    retry_at: Optional[datetime] = None,
    increment_attempts: bool = True,
) -> None:
    row = db.get(TelegramGatewayJob, int(job_id))
    if not row:
        return
    if increment_attempts:
        row.attempts = int(row.attempts or 0) + 1
    row.error_code = (error_code or "")[:64]
    row.error_message = (error_message or "")[:4000]
    row.updated_at = datetime.utcnow()
    max_attempts = 6
    if retry_at is not None and int(row.attempts) < max_attempts:
        row.status = "retry"
        row.run_after = retry_at
    else:
        row.status = "failed"
    db.flush()


def reset_stale_running_jobs(db, *, older_than_sec: Optional[int] = None) -> int:
    """
    Recover jobs stuck in ``running`` (worker crash / SIGKILL mid-job).
    Does not increment ``attempts`` — operator-visible code ``stale_running_recovered``.
    """
    sec = int(older_than_sec) if older_than_sec is not None else _running_stale_sec()
    cutoff = datetime.utcnow() - timedelta(seconds=sec)
    q = (
        db.query(TelegramGatewayJob)
        .filter(
            TelegramGatewayJob.status == "running",
            TelegramGatewayJob.updated_at < cutoff,
        )
        .all()
    )
    n = 0
    for row in q:
        row.status = "retry"
        row.run_after = datetime.utcnow()
        row.error_code = "stale_running_recovered"
        row.error_message = (
            f"Running state exceeded {sec}s without heartbeat; scheduled retry."
        )
        row.updated_at = datetime.utcnow()
        n += 1
    if n:
        db.flush()
    return n
