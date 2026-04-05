"""
Account warmup and story eligibility logic.

Separates:
- app-level readiness (DB status, session path)
- Telegram session readiness (canonical file exists)
- real story eligibility (warmup + precheck + not blocked)

Warmup status: new | warming | warmed | risky | blocked
Story precheck: unknown | allowed | rate_limited | frozen | restricted | telegram_denied | blocked | not_warmed | failed_check
"""
from datetime import datetime, timedelta

from src.utils.helpers import utc_now
from typing import Any, Optional

import structlog

logger = structlog.get_logger(__name__)

# Warmup statuses
WARMUP_NEW = "new"
WARMUP_WARMING = "warming"
WARMUP_WARMED = "warmed"
WARMUP_RISKY = "risky"
WARMUP_BLOCKED = "blocked"

# Story precheck statuses (align with dashboard)
PRECHECK_UNKNOWN = "unknown"
PRECHECK_ALLOWED = "allowed"
PRECHECK_RATE_LIMITED = "rate_limited"
PRECHECK_FROZEN = "frozen"
PRECHECK_RESTRICTED = "restricted"
PRECHECK_BLOCKED = "blocked"
PRECHECK_NOT_WARMED = "not_warmed"
PRECHECK_FAILED_CHECK = "failed_check"


def _get_settings():
    try:
        from config.settings import settings
        return settings
    except Exception:
        return None


def _parse_dt(val: Any) -> Optional[datetime]:
    if val is None:
        return None
    if hasattr(val, "year"):
        return val
    try:
        s = str(val).replace("Z", "").split("+")[0].strip()
        return datetime.fromisoformat(s)
    except Exception:
        return None


def get_warmup_status(account: Any) -> str:
    """
    Returns: new | warming | warmed | risky | blocked
    - new: just imported, never attempted story
    - warming: imported recently, within min_account_age; not yet warmed
    - warmed: past min_account_age or has successful_story_count > 0
    - risky: multiple failures, or story errors
    - blocked: warmup rules or story_status blocks use

    Reconciles DB warmup_status with imported_at: when DB says new/warming but
    the temporal gate (first_seen + min_account_age_hours) has passed, returns
    warmed so dashboard matches is_warmup_blocked (which uses timestamps).
    """
    ws = getattr(account, "warmup_status", None) or ""
    imported_at = _parse_dt(getattr(account, "imported_at", None))
    created_at = _parse_dt(getattr(account, "created_at", None))
    first_seen = imported_at or created_at
    success_count = getattr(account, "successful_story_count", None) or 0
    failed_count = getattr(account, "failed_story_count", None) or 0

    if success_count > 0:
        return WARMUP_WARMED
    if failed_count >= 3:
        return WARMUP_RISKY
    if ws in (WARMUP_RISKY, WARMUP_BLOCKED):
        return ws
    if first_seen is None:
        return WARMUP_WARMED  # Legacy account without imported_at
    cfg = _get_settings()
    if not cfg or not getattr(getattr(cfg, "warmup", None), "enabled", True):
        return WARMUP_WARMED
    min_hours = getattr(getattr(cfg, "warmup", None), "min_account_age_hours", 24) or 24
    until = first_seen + timedelta(hours=min_hours)
    gate_passed = utc_now() >= until
    if ws in (WARMUP_NEW, WARMUP_WARMING) and gate_passed:
        return WARMUP_WARMED
    if ws in (WARMUP_NEW, WARMUP_WARMING, WARMUP_WARMED):
        return ws
    cutoff = utc_now() - timedelta(hours=min_hours)
    if first_seen > cutoff:
        return WARMUP_WARMING
    return WARMUP_WARMED


def is_warmup_blocked(account: Any) -> tuple[bool, str]:
    """
    Returns (blocked: bool, reason: str).
    True when warmup rules prevent story posting.
    """
    cfg = _get_settings()
    if not cfg:
        return False, ""
    w = getattr(cfg, "warmup", None)
    if not w or not getattr(w, "enabled", True):
        return False, ""

    status = get_warmup_status(account)
    if status == WARMUP_BLOCKED:
        return True, "warmup_blocked"
    if status == WARMUP_NEW or status == WARMUP_WARMING:
        first_seen = _parse_dt(getattr(account, "imported_at", None)) or _parse_dt(getattr(account, "created_at", None))
        min_hours = getattr(w, "min_account_age_hours", 24) or 24
        if first_seen:
            until = first_seen + timedelta(hours=min_hours)
            if utc_now() < until:
                return True, f"warmup_wait_until_{until.isoformat()}"
    if status == WARMUP_RISKY:
        return True, "warmup_risky"
    return False, ""


def is_cooldown_blocked(account: Any) -> tuple[bool, str]:
    """Cooldown depends only on last_story_failure_at. No timestamp comparisons."""
    cfg = _get_settings()
    if not cfg:
        return False, ""
    w = getattr(cfg, "warmup", None)
    if not w:
        return False, ""
    failure_cooldown_min = getattr(w, "story_failure_cooldown_minutes", 120) or 120
    last_failure = _parse_dt(getattr(account, "last_story_failure_at", None))
    if not last_failure:
        return False, ""
    until = last_failure + timedelta(minutes=failure_cooldown_min)
    if utc_now() < until:
        return True, f"cooldown_until_{until.isoformat()}"
    return False, ""


def is_attempt_cap_exceeded(account: Any) -> bool:
    """Check max_story_attempts_per_day (story_attempts_today)."""
    cfg = _get_settings()
    if not cfg:
        return False
    w = getattr(cfg, "warmup", None)
    if not w:
        return False
    cap = getattr(w, "max_story_attempts_per_day", 5) or 0
    if cap <= 0:
        return False
    attempts = getattr(account, "story_attempts_today", None) or 0
    return attempts >= cap


def is_daily_cap_exceeded(account: Any) -> bool:
    """Check max_story_successes_per_day (stories_today counts successes only)."""
    cfg = _get_settings()
    if not cfg:
        return False
    w = getattr(cfg, "warmup", None)
    if not w:
        return False
    cap = getattr(w, "max_story_successes_per_day", 3) or 0
    if cap <= 0:
        return False
    stories_today = getattr(account, "stories_today", None) or 0
    return stories_today >= cap


def get_safe_jitter_sec() -> tuple[int, int]:
    """Return (min_sec, max_sec) for random jitter between bulk actions."""
    cfg = _get_settings()
    if not cfg:
        return 10, 90
    w = getattr(cfg, "warmup", None)
    if not w:
        return 10, 90
    return getattr(w, "jitter_min_sec", 10) or 10, getattr(w, "jitter_max_sec", 90) or 90


def format_warmup_for_ui(account: Any) -> dict:
    """Return {warmup_status, warmup_label, is_blocked, block_reason} for dashboard."""
    status = get_warmup_status(account)
    blocked, reason = is_warmup_blocked(account)
    if not blocked:
        blocked, reason = is_cooldown_blocked(account)
    labels = {
        WARMUP_NEW: "New",
        WARMUP_WARMING: "Warming",
        WARMUP_WARMED: "Warmed",
        WARMUP_RISKY: "Risky",
        WARMUP_BLOCKED: "Blocked",
    }
    return {
        "warmup_status": status,
        "warmup_label": labels.get(status, status),
        "is_warmup_blocked": blocked,
        "warmup_block_reason": reason or "",
    }
