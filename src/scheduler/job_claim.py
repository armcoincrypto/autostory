"""
Atomic DB lease claim for scheduled job execution (one row per call).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.core.scheduler_models import JobStatus

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ClaimResult:
    job_id: Optional[int]
    lease_until: Optional[datetime]
    stale_reclaim: bool


def claim_due_job(
    db: Session,
    *,
    worker_id: str,
    lease_seconds: int,
    now_naive: datetime,
) -> ClaimResult:
    """
    Atomically claim one due job: PENDING with run_at <= now, else stale RUNNING
    (lease_until < now). Returns ClaimResult with job_id None if nothing claimed.
    """
    if lease_seconds < 1:
        lease_seconds = 1
    lease_until = now_naive + timedelta(seconds=int(lease_seconds))
    updated_at = now_naive
    pend = JobStatus.PENDING.value
    run = JobStatus.RUNNING.value

    # Prefer fresh PENDING work over reclaiming stale RUNNING.
    row = db.execute(
        text(
            """
            UPDATE scheduled_jobs
            SET status = :running,
                lease_until = :lease_until,
                lease_owner = :lease_owner,
                updated_at = :updated_at
            WHERE rowid = (
                SELECT sj.rowid
                FROM scheduled_jobs AS sj
                WHERE sj.status = :pending
                  AND sj.run_at <= :now_naive
                ORDER BY sj.run_at ASC, sj.id ASC
                LIMIT 1
            )
            RETURNING id
            """
        ),
        {
            "running": run,
            "lease_until": lease_until,
            "lease_owner": worker_id,
            "updated_at": updated_at,
            "pending": pend,
            "now_naive": now_naive,
        },
    ).fetchone()

    if row:
        jid = int(row[0])
        logger.info(
            "scheduler_job_claimed",
            job_id=jid,
            lease_owner=worker_id,
            lease_until=lease_until,
        )
        return ClaimResult(job_id=jid, lease_until=lease_until, stale_reclaim=False)

    row2 = db.execute(
        text(
            """
            UPDATE scheduled_jobs
            SET status = :running,
                lease_until = :lease_until,
                lease_owner = :lease_owner,
                updated_at = :updated_at
            WHERE rowid = (
                SELECT sj.rowid
                FROM scheduled_jobs AS sj
                WHERE sj.status = :running
                  AND sj.lease_until IS NOT NULL
                  AND sj.lease_until < :now_naive
                ORDER BY sj.lease_until ASC, sj.id ASC
                LIMIT 1
            )
            RETURNING id
            """
        ),
        {
            "running": run,
            "lease_until": lease_until,
            "lease_owner": worker_id,
            "updated_at": updated_at,
            "now_naive": now_naive,
        },
    ).fetchone()

    if row2:
        jid = int(row2[0])
        logger.info(
            "scheduler_job_stale_reclaimed",
            job_id=jid,
            lease_owner=worker_id,
            lease_until=lease_until,
        )
        return ClaimResult(job_id=jid, lease_until=lease_until, stale_reclaim=True)

    logger.debug("scheduler_job_claim_skip", lease_owner=worker_id)
    return ClaimResult(job_id=None, lease_until=None, stale_reclaim=False)
