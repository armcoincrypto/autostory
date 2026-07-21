"""P10.11 Stories runtime validation (safe/read-only planning)."""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from src.core.models import Account, AccountStatus, StoryRun, StoryRunStep


def validate_stories_runtime(db: Session, *, account_ids: list[int] | None = None) -> dict[str, Any]:
    q = db.query(Account).order_by(Account.id.asc())
    if account_ids:
        q = q.filter(Account.id.in_([int(x) for x in account_ids]))
    candidates = []
    for acc in q.all():
        status = acc.status.value if hasattr(acc.status, "value") else str(acc.status or "")
        purpose = (acc.purpose or "").strip().lower() or "both"
        blockers = []
        if status != AccountStatus.ACTIVE.value:
            blockers.append("account_not_active")
        if purpose not in ("both", "autostory", ""):
            blockers.append("purpose_not_story_compatible")
        if (acc.story_precheck_status or "").strip().lower() not in ("allowed", ""):
            blockers.append("story_precheck_blocked")
        candidates.append({"account_id": int(acc.id), "usable": not blockers, "blockers": blockers})

    runs = db.query(StoryRun).order_by(StoryRun.id.desc()).limit(50).all()
    active_runs = [r for r in runs if str(r.status).lower() in ("pending", "running")]
    recent_steps = db.query(StoryRunStep).order_by(StoryRunStep.id.desc()).limit(50).all()
    return {
        "track": "B",
        "outcome": "STORIES_RUNTIME_READY",
        "candidate_accounts": candidates,
        "active_run_count": len(active_runs),
        "active_runs": [
            {
                "run_id": int(r.id),
                "status": r.status,
                "mode": r.mode,
                "stories_ok": int(r.stories_ok or 0),
                "stories_failed": int(r.stories_failed or 0),
            }
            for r in active_runs
        ],
        "recent_step_states": [
            {"step_id": int(s.id), "run_id": int(s.run_id), "account_id": int(s.account_id), "status": s.status}
            for s in recent_steps
        ],
        "safety_note": "No story publishing is performed by P10.11 validation.",
    }
