"""P10.11 runtime observability projections for operator reports."""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from src.ai_agent.account_allowlist import RESERVED_AI_AGENT_ACCOUNT_IDS
from src.core.account_operational_state import CONTROLLER_ACCOUNT_IDS, compute_account_operational_state
from src.core.models import Account
from src.core.scheduler_models import JobStatus, ScheduledJob
from src.dashboard.scheduler_mutations import scheduler_mutations_enabled
from src.recovery.p9_83_governance_observability import PROTECTED_IDS, PURPOSE_HOLD_IDS
from src.scheduler.campaign_governance import campaign_execution_enabled


def scheduler_state_for_job(job: ScheduledJob, *, now: datetime | None = None) -> str:
    status = str(job.status or "").upper()
    now = now or datetime.utcnow()
    if status == JobStatus.RUNNING.value:
        if job.lease_until is None or job.lease_until <= now:
            return "STALE"
        return "RUNNING"
    if status == JobStatus.PENDING.value:
        return "PENDING"
    if status in (JobStatus.SENT.value, JobStatus.SKIPPED.value, JobStatus.CANCELLED.value):
        return "COMPLETE"
    if status == JobStatus.FAILED.value:
        return "ERROR"
    return "IDLE"


def account_runtime_state(db: Session, account: Account) -> dict[str, Any]:
    aid = int(account.id)
    op = compute_account_operational_state(db, account)
    active_jobs = (
        db.query(ScheduledJob)
        .filter(
            ScheduledJob.account_id == aid,
            ScheduledJob.status.in_([JobStatus.PENDING.value, JobStatus.RUNNING.value]),
        )
        .count()
    )
    if aid in PROTECTED_IDS or aid in CONTROLLER_ACCOUNT_IDS:
        state = "PROTECTED"
    elif aid in PURPOSE_HOLD_IDS:
        state = "HELD"
    elif active_jobs:
        state = "EXECUTING"
    elif aid in RESERVED_AI_AGENT_ACCOUNT_IDS:
        state = "GOVERNED"
    elif op.get("scheduler_eligible") and op.get("discovery_eligible"):
        state = "OPERATIONAL"
    elif op.get("readiness_status") == "READY" and not op.get("resolver_code"):
        state = "TECH_HEALTHY"
    else:
        state = "ERROR"
    return {
        "account_id": aid,
        "runtime_state": state,
        "purpose": (account.purpose or "").strip().lower() or None,
        "scheduler_eligible": op.get("scheduler_eligible"),
        "discovery_eligible": op.get("discovery_eligible"),
        "readiness_status": op.get("readiness_status"),
        "resolver_code": op.get("resolver_code"),
        "active_jobs": int(active_jobs),
    }


def build_runtime_observability_snapshot(
    db: Session,
    *,
    account_ids: list[int] | None = None,
) -> dict[str, Any]:
    now = datetime.utcnow()
    account_query = db.query(Account).order_by(Account.id.asc())
    if account_ids:
        account_query = account_query.filter(Account.id.in_([int(x) for x in account_ids]))
    account_rows = [account_runtime_state(db, acc) for acc in account_query.all()]

    job_rows = []
    for job in db.query(ScheduledJob).order_by(ScheduledJob.id.asc()).all():
        job_rows.append(
            {
                "job_id": int(job.id),
                "account_id": int(job.account_id),
                "target_id": int(job.target_id),
                "status": str(job.status),
                "runtime_state": scheduler_state_for_job(job, now=now),
                "lease_owner": job.lease_owner,
                "lease_until": job.lease_until.isoformat() if job.lease_until else None,
                "attempts": int(job.attempts or 0),
                "last_error": job.last_error,
            }
        )

    return {
        "track": "F",
        "checked_at": now.isoformat(),
        "scheduler_global_state": "LOCKED" if not scheduler_mutations_enabled() else "UNLOCKED",
        "campaign_execution_state": "LOCKED" if not campaign_execution_enabled() else "UNLOCKED",
        "account_state_counts": dict(Counter(r["runtime_state"] for r in account_rows)),
        "scheduler_state_counts": dict(Counter(r["runtime_state"] for r in job_rows)),
        "accounts": account_rows,
        "jobs": job_rows,
        "ui_copy": [
            "Scheduler eligible does not bypass the global scheduler lock.",
            "Campaigns require governance approval.",
            "Runtime state is separate from session/auth health.",
        ],
    }
