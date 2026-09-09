"""Scheduled DM product flags (Wave 10 + Wave D scoping).

Creation/cancel require:
- SCHEDULED_DM_ENABLED (product rail)

Wave D: scheduled DM is independent of SCHEDULER_MUTATIONS_ENABLED so a DM
canary does not unlock PROMO/INFO mutation paths.

Execution of already-PENDING DM jobs is independent (worker + Owner DM
MESSAGES_EXECUTION_ENABLED still apply at send time).
"""
from __future__ import annotations

import os

from config.settings import settings


def scheduled_dm_enabled() -> bool:
    """Fail-closed product flag for scheduling private DMs."""
    env = (os.environ.get("SCHEDULED_DM_ENABLED") or "").strip().lower()
    if env:
        return env in {"1", "true", "yes", "on"}
    return bool(getattr(settings, "scheduled_dm_enabled", False))


def scheduled_dm_create_allowed() -> bool:
    """True when the scheduled-DM product rail is open (Wave D: not tied to global scheduler mutations)."""
    return scheduled_dm_enabled()


def scheduled_dm_deny_payload() -> dict:
    from src.dashboard.scheduler_mutations import (
        scheduler_info_mutations_allowed,
        scheduler_mutations_enabled,
        scheduler_promo_mutations_allowed,
    )

    return {
        "ok": False,
        "error": "SCHEDULED_DM_DISABLED",
        "error_code": "SCHEDULED_DM_DISABLED",
        "message": (
            "Scheduled direct messages are disabled. "
            "Require SCHEDULED_DM_ENABLED=true "
            "(independent of SCHEDULER_MUTATIONS_ENABLED / PROMO / INFO flags)."
        ),
        "scheduled_dm_enabled": scheduled_dm_enabled(),
        "scheduler_mutations_enabled": scheduler_mutations_enabled(),
        "scheduler_promo_mutations_allowed": scheduler_promo_mutations_allowed(),
        "scheduler_info_mutations_allowed": scheduler_info_mutations_allowed(),
    }
