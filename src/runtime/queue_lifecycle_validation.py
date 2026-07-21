"""P10.11 queue lifecycle validation.

Read-only by default. This module inspects scheduler jobs and delivery intent rows for
stale leases, orphan RUNNING jobs, duplicate active work, and retry guard violations.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from src.core.scheduler_models import DeliveryStatus, JobStatus, MessageDelivery, ScheduledJob


ACTIVE_JOB_STATUSES = (JobStatus.PENDING.value, JobStatus.RUNNING.value)
TERMINAL_JOB_STATUSES = (
    JobStatus.SENT.value,
    JobStatus.FAILED.value,
    JobStatus.SKIPPED.value,
    JobStatus.CANCELLED.value,
)


@dataclass(frozen=True)
class QueueAuditConfig:
    stale_running_minutes: int = 30
    stale_sending_minutes: int = 30
    max_attempts: int = 3


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _job_row(job: ScheduledJob, *, now: datetime) -> dict[str, Any]:
    lease_active = bool(job.lease_until and job.lease_until > now)
    age_sec = int((now - (job.updated_at or job.created_at or now)).total_seconds())
    return {
        "job_id": int(job.id),
        "account_id": int(job.account_id),
        "target_id": int(job.target_id),
        "type": job.type,
        "status": str(job.status),
        "attempts": int(job.attempts or 0),
        "last_error": job.last_error,
        "run_at": _iso(job.run_at),
        "lease_owner": job.lease_owner,
        "lease_until": _iso(job.lease_until),
        "lease_active": lease_active,
        "updated_at": _iso(job.updated_at),
        "created_at": _iso(job.created_at),
        "age_sec": age_sec,
    }


def audit_queue_lifecycle(
    db: Session,
    *,
    config: QueueAuditConfig | None = None,
) -> dict[str, Any]:
    cfg = config or QueueAuditConfig()
    now = datetime.utcnow()
    stale_cutoff = now - timedelta(minutes=cfg.stale_running_minutes)
    sending_cutoff = now - timedelta(minutes=cfg.stale_sending_minutes)

    active_jobs = (
        db.query(ScheduledJob)
        .filter(ScheduledJob.status.in_(ACTIVE_JOB_STATUSES))
        .order_by(ScheduledJob.id.asc())
        .all()
    )
    active_rows = [_job_row(j, now=now) for j in active_jobs]

    orphan_running: list[dict[str, Any]] = []
    stale_leases: list[dict[str, Any]] = []
    retry_exceeded: list[dict[str, Any]] = []
    for job in active_jobs:
        row = _job_row(job, now=now)
        status = str(job.status)
        if status == JobStatus.RUNNING.value:
            lease_expired = job.lease_until is None or job.lease_until <= now
            updated_stale = (job.updated_at or job.created_at or now) <= stale_cutoff
            if lease_expired or updated_stale:
                orphan_running.append({**row, "reason": "running_without_active_lease_or_stale_update"})
        if job.lease_until is not None and job.lease_until <= now:
            stale_leases.append({**row, "reason": "lease_expired"})
        if int(job.attempts or 0) > cfg.max_attempts:
            retry_exceeded.append({**row, "reason": "attempts_above_max"})

    duplicate_active: list[dict[str, Any]] = []
    grouped: dict[tuple[int, int, str], list[dict[str, Any]]] = defaultdict(list)
    for row in active_rows:
        grouped[(int(row["account_id"]), int(row["target_id"]), str(row["type"]))].append(row)
    for key, rows in grouped.items():
        if len(rows) > 1:
            duplicate_active.append(
                {
                    "account_id": key[0],
                    "target_id": key[1],
                    "type": key[2],
                    "job_ids": [int(r["job_id"]) for r in rows],
                }
            )

    sending_rows = (
        db.query(MessageDelivery)
        .filter(MessageDelivery.status == DeliveryStatus.SENDING.value)
        .order_by(MessageDelivery.id.asc())
        .all()
    )
    stale_sending = []
    for row in sending_rows:
        created = row.attempt_started_at or row.created_at
        if created is None or created <= sending_cutoff:
            stale_sending.append(
                {
                    "delivery_id": int(row.id),
                    "job_id": int(row.job_id) if row.job_id is not None else None,
                    "account_id": int(row.account_id),
                    "target_id": int(row.target_id),
                    "created_at": _iso(row.created_at),
                    "attempt_started_at": _iso(row.attempt_started_at),
                    "reason": "stale_sending_intent",
                }
            )

    terminal_counts = {
        status: db.query(ScheduledJob).filter(ScheduledJob.status == status).count()
        for status in TERMINAL_JOB_STATUSES
    }
    blockers = []
    if orphan_running:
        blockers.append("orphan_running_jobs")
    if stale_leases:
        blockers.append("stale_leases")
    if duplicate_active:
        blockers.append("duplicate_active_jobs")
    if stale_sending:
        blockers.append("stale_sending_deliveries")
    if retry_exceeded:
        blockers.append("retry_attempts_exceeded")

    return {
        "track": "D",
        "outcome": "QUEUE_LIFECYCLE_OK" if not blockers else "QUEUE_LIFECYCLE_BLOCKED",
        "checked_at": now.isoformat(),
        "active_jobs": active_rows,
        "active_job_count": len(active_rows),
        "orphan_running": orphan_running,
        "stale_leases": stale_leases,
        "duplicate_active": duplicate_active,
        "stale_sending": stale_sending,
        "retry_exceeded": retry_exceeded,
        "terminal_counts": terminal_counts,
        "blockers": blockers,
        "repair_note": "P10.11 is validation-only; repair requires an explicit cleanup phase.",
    }
