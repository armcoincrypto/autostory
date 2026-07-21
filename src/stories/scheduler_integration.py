"""Purpose-separated Story execution gates.

Controlled web publication and scheduler mutation deliberately use independent
flags. The legacy ``STORY_EXECUTION_ENABLED`` flag is observation-only and
never authorizes either path.
"""
from __future__ import annotations

import os
from typing import Any


_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
CONTROLLED_EXECUTION_FLAG = "CONTROLLED_STORY_EXECUTION_ENABLED"
CONTROLLED_ACCOUNT_FLAG = "CONTROLLED_STORY_ACCOUNT_ID"
SCHEDULER_EXECUTION_FLAG = "SCHEDULER_STORY_EXECUTION_ENABLED"


def _enabled(name: str) -> bool:
    return os.environ.get(name, "false").strip().lower() in _TRUE_VALUES


def controlled_story_execution_enabled() -> bool:
    return _enabled(CONTROLLED_EXECUTION_FLAG)


def scheduler_story_execution_enabled() -> bool:
    return _enabled(SCHEDULER_EXECUTION_FLAG)


def controlled_story_execution_allowed(account_id: int | None) -> tuple[bool, str]:
    if not controlled_story_execution_enabled():
        return False, "controlled_story_execution_disabled"
    configured = os.environ.get(CONTROLLED_ACCOUNT_FLAG, "").strip()
    if account_id is None or configured != str(int(account_id)):
        return False, "controlled_story_account_mismatch"
    return True, "controlled_story_publish_ok"


def story_execution_enabled() -> bool:
    """Compatibility alias for scheduler callers; never enables controlled live."""
    return scheduler_story_execution_enabled()


def build_story_scheduler_integration_map() -> dict[str, Any]:
    enabled = scheduler_story_execution_enabled()
    return {
        "story_execution_enabled": enabled,
        "controlled_story_execution_enabled": controlled_story_execution_enabled(),
        "env_flag": SCHEDULER_EXECUTION_FLAG,
        "controlled_env_flag": CONTROLLED_EXECUTION_FLAG,
        "default": "false",
        "worker_loop": "src.scheduler.worker.run_scheduler_loop",
        "engine": "disabled_in_clean_runtime_safety_phase",
        "current_state": (
            "disabled_no_live_story_execution"
            if not enabled
            else "blocked_pending_certified_scheduler_contract"
        ),
        "status_transitions": {},
        "safety": {
            "controlled_and_scheduler_flags_separate": True,
            "scheduler_mutation_hard_disabled": True,
            "precheck_and_dry_run_do_not_publish": True,
        },
    }


async def maybe_tick_story_rotation() -> dict[str, Any]:
    """Fail closed: scheduler Story mutation is not certified in this phase."""
    integration = build_story_scheduler_integration_map()
    if not integration["story_execution_enabled"]:
        return {
            "skipped": True,
            "reason": "scheduler_story_execution_disabled",
            "integration": integration,
        }
    return {
        "skipped": True,
        "reason": "scheduler_story_execution_not_certified",
        "integration": integration,
    }
