"""Purpose-separated Story execution gates.

Controlled web publication and scheduler mutation deliberately use independent
flags. The legacy ``STORY_EXECUTION_ENABLED`` flag is observation-only and
never authorizes either path.

Scheduler Story path (when enabled) only ticks Auto Story campaigns — legacy
rotation remains uncertified.
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
    """Allow controlled live when flag is on and account is authorized.

    Authorization: env mutation allowlist, OR durable campaign wave scope,
    OR legacy single ``CONTROLLED_STORY_ACCOUNT_ID`` when allowlist empty.
    """
    if not controlled_story_execution_enabled():
        return False, "controlled_story_execution_disabled"
    if account_id is None:
        return False, "controlled_story_account_mismatch"

    from src.stories.mutation_boundary import parse_story_mutation_allowlist

    allowlist = parse_story_mutation_allowlist()
    if allowlist and int(account_id) in allowlist:
        return True, "controlled_story_publish_ok"

    try:
        from src.stories.autostory_hardening import account_in_active_wave_authorization

        if account_in_active_wave_authorization(int(account_id)):
            return True, "controlled_story_publish_ok_campaign_wave"
    except Exception:
        pass

    if allowlist:
        return False, "controlled_story_account_mismatch"

    configured = os.environ.get(CONTROLLED_ACCOUNT_FLAG, "").strip()
    if configured != str(int(account_id)):
        return False, "controlled_story_account_mismatch"
    return True, "controlled_story_publish_ok"


def controlled_story_accounts_execution_allowed(
    account_ids: list[int] | None,
) -> tuple[bool, str, list[int]]:
    """Multi-account variant of ``controlled_story_execution_allowed``."""
    if not controlled_story_execution_enabled():
        return False, "controlled_story_execution_disabled", list(account_ids or [])
    ids = [int(x) for x in (account_ids or [])]
    if not ids:
        return False, "controlled_story_account_mismatch", []
    denied: list[int] = []
    for aid in ids:
        ok, reason = controlled_story_execution_allowed(aid)
        if not ok:
            denied.append(aid)
            return False, reason, denied
    return True, "controlled_story_publish_ok", []


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
        "engine": "auto_story_campaigns",
        "current_state": (
            "disabled_no_live_story_execution"
            if not enabled
            else "auto_story_campaign_tick_enabled"
        ),
        "status_transitions": {
            "draft": ["active", "cancelled"],
            "active": ["paused", "completed", "cancelled"],
            "paused": ["active", "cancelled"],
        },
        "safety": {
            "controlled_and_scheduler_flags_separate": True,
            "scheduler_requires_mutations_and_allowlist": True,
            "precheck_and_dry_run_do_not_publish": True,
            "manual_first_wave_recommended": True,
        },
    }


async def maybe_tick_story_rotation() -> dict[str, Any]:
    """Tick due Auto Story campaigns when scheduler Story execution is enabled."""
    integration = build_story_scheduler_integration_map()
    if not integration["story_execution_enabled"]:
        return {
            "skipped": True,
            "reason": "scheduler_story_execution_disabled",
            "integration": integration,
        }
    from src.stories.auto_story_service import tick_due_auto_story_campaigns

    tick = tick_due_auto_story_campaigns()
    return {
        "skipped": bool(tick.get("skipped")),
        "reason": tick.get("reason"),
        "integration": integration,
        "auto_story": tick,
    }
