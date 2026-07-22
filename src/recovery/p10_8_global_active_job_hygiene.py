"""Dashboard usability fields without the full recovery-lab dependency tree."""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from src.core.models import Account


def build_account_usability_fields(db: Session, account_id: int) -> dict[str, Any]:
    aid = int(account_id)
    account = db.query(Account).filter(Account.id == aid).first()
    health = (getattr(account, "health_status", None) or "").strip().lower() if account else ""
    technical = "ok" if health in ("", "alive", "ok") else "needs_repair"
    active_job_count = 0
    try:
        from src.core.scheduler_models import JobStatus, ScheduledJob

        active_job_count = (
            db.query(ScheduledJob)
            .filter(
                ScheduledJob.account_id == aid,
                ScheduledJob.status.in_([JobStatus.PENDING.value, JobStatus.RUNNING.value]),
            )
            .count()
        )
    except Exception:
        active_job_count = 0
    return {
        "account_id": aid,
        "technical_health_label": technical,
        "active_job_count": int(active_job_count),
        "scheduler_queue_blocker": None,
        "compatibility_shim": True,
    }
