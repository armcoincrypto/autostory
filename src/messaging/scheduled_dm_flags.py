"""Scheduled DM product flags (Wave 10).

Creation/cancel require BOTH:
- SCHEDULED_DM_ENABLED (product rail)
- SCHEDULER_MUTATIONS_ENABLED (queue write kill switch)

Execution of already-PENDING DM jobs is independent (worker + Owner DM
MESSAGES_EXECUTION_ENABLED still apply at send time).
"""
from __future__ import annotations

import os

from config.settings import settings
from src.dashboard.scheduler_mutations import scheduler_mutations_enabled


def scheduled_dm_enabled() -> bool:
    """Fail-closed product flag for scheduling private DMs."""
    env = (os.environ.get("SCHEDULED_DM_ENABLED") or "").strip().lower()
    if env:
        return env in {"1", "true", "yes", "on"}
    return bool(getattr(settings, "scheduled_dm_enabled", False))


def scheduled_dm_create_allowed() -> bool:
    """True only when both product and scheduler-mutation gates are open."""
    return scheduled_dm_enabled() and scheduler_mutations_enabled()


def scheduled_dm_deny_payload() -> dict:
    return {
        "ok": False,
        "error": "SCHEDULED_DM_DISABLED",
        "error_code": "SCHEDULED_DM_DISABLED",
        "message": (
            "Scheduled direct messages are disabled. "
            "Require SCHEDULED_DM_ENABLED=true and SCHEDULER_MUTATIONS_ENABLED=true."
        ),
        "scheduled_dm_enabled": scheduled_dm_enabled(),
        "scheduler_mutations_enabled": scheduler_mutations_enabled(),
    }
