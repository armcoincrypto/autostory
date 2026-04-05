"""
STORYFLEET Telegram Trust Preservation - Central Safety Policy Engine.

Single source of truth for story eligibility and operator protection.
Principles:
- Alive != story-ready
- Session-ready != trusted for stories
- Prefer false-negative over risky false-positive
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from src.utils.helpers import utc_now
from typing import Any, Optional

import structlog

logger = structlog.get_logger(__name__)

# Risk levels (operator-facing)
RISK_SAFE = "safe"
RISK_WARMING = "warming"
RISK_RISKY = "risky"
RISK_BLOCKED = "blocked"

# Reason codes (machine-facing)
REASON_NO_SESSION = "no_session"
REASON_AUTH_REQUIRED = "auth_required"
REASON_WARMUP_PENDING = "warmup_pending"
REASON_STORY_PRECHECK_FAILED = "story_precheck_failed"
REASON_STORY_RATE_LIMITED = "story_rate_limited"
REASON_STORY_FROZEN = "story_frozen"
REASON_STORY_RESTRICTED = "story_restricted"
REASON_STORY_BLOCKED = "story_blocked"
REASON_STORY_TELEGRAM_DENIED = "story_telegram_denied"
REASON_TOO_MANY_PROFILE_CHANGES = "too_many_recent_profile_changes"
REASON_TOO_MANY_USERNAME_CHANGES = "too_many_recent_username_changes"
REASON_TOO_MANY_STORY_ATTEMPTS = "too_many_recent_story_attempts"
REASON_TOO_NEW_AFTER_IMPORT = "too_new_after_import"
REASON_MANUAL_REVIEW_REQUIRED = "manual_review_required"
REASON_COOLDOWN = "story_cooldown"
REASON_DAILY_CAP = "daily_cap"


@dataclass
class StorySafetyDecision:
    """Result of get_story_safety_decision."""
    allowed: bool
    reason_code: str
    human_reason: str
    next_allowed_at: Optional[datetime] = None
    risk_level: str = RISK_SAFE
    operator_action: str = ""
    precheck_overrode_stale: bool = False


def _get_settings() -> Any:
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


def get_account_risk_level(account: Any) -> str:
    """
    Returns: safe | warming | risky | blocked
    """
    try:
        from src.core.warmup import get_warmup_status
        status = get_warmup_status(account)
    except Exception:
        status = "warmed"

    if status in ("blocked",):
        return RISK_BLOCKED
    if status in ("new", "warming"):
        return RISK_WARMING
    if status == "risky":
        return RISK_RISKY
    return RISK_SAFE


def get_story_safety_decision(
    account: Any,
    requested_action: str = "story_publish",
    _canonical_exists: set[int] | None = None,
) -> StorySafetyDecision:
    """
    Single source of truth for story eligibility. Checks in order:
    1. canonical session exists
    2. account active/auth-valid
    3. story precheck acceptable (or stale = require refresh)
    4. warmup rules pass
    5. cooldown rules pass
    6. daily cap not exceeded
    7. bulk risk rules
    8. manual review not required

    When _canonical_exists is provided (from get_existing_canonical_account_ids), avoids disk stat.
    """
    cfg = _get_settings()
    now = utc_now()

    # 1. Canonical session
    try:
        from src.core.session_paths import account_has_canonical_session
        has_session = account_has_canonical_session(account, _canonical_exists=_canonical_exists)
    except Exception:
        has_session = False
    if not has_session:
        return StorySafetyDecision(
            allowed=False,
            reason_code=REASON_NO_SESSION,
            human_reason="No session file. Re-import via TDATA or session string.",
            risk_level=RISK_BLOCKED,
            operator_action="Re-import session",
        )

    # 2. Account active
    st = getattr(account, "status", None)
    st_val = (st.value if hasattr(st, "value") else str(st or "")) or ""
    if st_val not in ("active",):
        return StorySafetyDecision(
            allowed=False,
            reason_code=REASON_AUTH_REQUIRED,
            human_reason=f"Account status={st_val}. Run health check or re-login.",
            risk_level=RISK_BLOCKED,
            operator_action="Check account health",
        )

    hs = getattr(account, "health_status", None)
    if hs not in (None, "alive"):
        return StorySafetyDecision(
            allowed=False,
            reason_code=REASON_AUTH_REQUIRED,
            human_reason=f"Health status={hs or 'unknown'}. Alive != story-ready.",
            risk_level=RISK_BLOCKED,
            operator_action="Run health check",
        )

    # 3. Story precheck (posting uses stricter TTL)
    precheck = getattr(account, "story_precheck_status", None) or "unknown"
    precheck_checked_at = _parse_dt(getattr(account, "story_precheck_checked_at", None))
    cfg_warmup = getattr(cfg, "warmup", None)
    if requested_action == "story_publish":
        precheck_ttl_min = getattr(cfg_warmup, "precheck_ttl_post_minutes", 15) or 15
    else:
        precheck_ttl_min = getattr(cfg_warmup, "precheck_ttl_minutes", 1440) or 1440
    precheck_stale = precheck_checked_at is None or (now - precheck_checked_at).total_seconds() > (precheck_ttl_min * 60)

    _BLOCKING_PRECHECK = frozenset({
        "frozen",
        "restricted",
        "telegram_denied",
        "rate_limited",
        "failed_check",
        "blocked",
    })
    # DB story_status uses a smaller vocabulary than story_precheck_status (no telegram_denied).
    _STORY_STATUS_BLOCKING = frozenset({"frozen", "restricted", "rate_limited", "failed_check", "blocked"})
    if precheck in _BLOCKING_PRECHECK:
        reason_map = {
            "frozen": (
                "Telegram reports this account is frozen for stories (high-confidence check).",
                REASON_STORY_FROZEN,
            ),
            "restricted": (
                "Story publish is restricted by Telegram for this account.",
                REASON_STORY_RESTRICTED,
            ),
            "telegram_denied": (
                "Story check returned a possibly frozen- or restricted-like Telegram error. "
                "See precheck raw error; re-run story precheck later.",
                REASON_STORY_TELEGRAM_DENIED,
            ),
            "rate_limited": ("Rate limited. Check story_blocked_until.", REASON_STORY_RATE_LIMITED),
            "failed_check": (
                "Story precheck did not succeed. Run story precheck again.",
                REASON_STORY_PRECHECK_FAILED,
            ),
            "blocked": (
                "Story publish blocked by Telegram. Re-run story precheck later.",
                REASON_STORY_BLOCKED,
            ),
        }
        hr, code = reason_map.get(precheck, ("Precheck blocked.", REASON_STORY_PRECHECK_FAILED))
        sb = getattr(account, "story_blocked_until", None)
        next_at = _parse_dt(sb) if sb and _parse_dt(sb) and _parse_dt(sb) > now else None
        return StorySafetyDecision(
            allowed=False,
            reason_code=code,
            human_reason=hr,
            next_allowed_at=next_at,
            risk_level=RISK_BLOCKED,
            operator_action="Wait or run story precheck",
        )

    # Stale precheck: block regardless of allowed/unknown. Stale "allowed" could be from days ago.
    if precheck_stale:
        return StorySafetyDecision(
            allowed=False,
            reason_code=REASON_STORY_PRECHECK_FAILED,
            human_reason="Precheck expired. Run story precheck first." if precheck == "allowed" else "Precheck never run or expired. Run story precheck first.",
            risk_level=RISK_WARMING,
            operator_action="Run story precheck",
        )

    # Precheck allowed overrides stale story_status/blocked_until (only when not stale)
    ss = getattr(account, "story_status", None) or "unknown"
    sb = getattr(account, "story_blocked_until", None)
    sb_dt = _parse_dt(sb)
    precheck_overrode = precheck == "allowed" and (ss in ("rate_limited", "frozen", "restricted") or (sb_dt and sb_dt > now))
    if precheck != "allowed" and (ss in _STORY_STATUS_BLOCKING or (sb_dt and sb_dt > now)):
        return StorySafetyDecision(
            allowed=False,
            reason_code=REASON_STORY_RATE_LIMITED if ss == "rate_limited" or sb_dt else REASON_STORY_FROZEN,
            human_reason=f"Story status={ss}. Run story precheck to refresh.",
            next_allowed_at=sb_dt if sb_dt and sb_dt > now else None,
            risk_level=RISK_BLOCKED,
            operator_action="Run story precheck",
        )

    # 4. Warmup
    try:
        from src.core.warmup import is_warmup_blocked
        warmup_blocked, warmup_reason = is_warmup_blocked(account)
    except Exception:
        warmup_blocked, warmup_reason = False, ""
    if warmup_blocked:
        return StorySafetyDecision(
            allowed=False,
            reason_code=REASON_WARMUP_PENDING,
            human_reason=warmup_reason or "Warmup pending. New imports must wait.",
            risk_level=RISK_WARMING,
            operator_action="Wait for warmup period",
        )

    # 5. Cooldown
    try:
        from src.core.warmup import is_cooldown_blocked
        cooldown_blocked, _ = is_cooldown_blocked(account)
    except Exception:
        cooldown_blocked = False
    if cooldown_blocked:
        last_f = _parse_dt(getattr(account, "last_story_failure_at", None))
        cfg_w = getattr(cfg, "warmup", None)
        cooldown_min = getattr(cfg_w, "story_failure_cooldown_minutes", 120) or 120
        next_at = (last_f + timedelta(minutes=cooldown_min)) if last_f else None
        return StorySafetyDecision(
            allowed=False,
            reason_code=REASON_COOLDOWN,
            human_reason=f"Cooldown after last attempt. Wait {cooldown_min} min.",
            next_allowed_at=next_at,
            risk_level=RISK_SAFE,
            operator_action="Wait for cooldown",
        )

    # 6. Attempt cap (max_story_attempts_per_day)
    try:
        from src.core.warmup import is_attempt_cap_exceeded
        attempt_cap_exceeded = is_attempt_cap_exceeded(account)
    except Exception:
        attempt_cap_exceeded = False
    if attempt_cap_exceeded:
        return StorySafetyDecision(
            allowed=False,
            reason_code=REASON_TOO_MANY_STORY_ATTEMPTS,
            human_reason="Max story attempts per day exceeded.",
            next_allowed_at=utc_now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1),
            risk_level=RISK_SAFE,
            operator_action="Try tomorrow",
        )

    # 7. Manual review required
    if getattr(account, "manual_review_required", False):
        reason = getattr(account, "manual_review_reason", None) or "Manual review required before story use."
        return StorySafetyDecision(
            allowed=False,
            reason_code=REASON_MANUAL_REVIEW_REQUIRED,
            human_reason=reason,
            risk_level=RISK_RISKY,
            operator_action="Review account and clear manual_review_required",
        )
    manual_req, manual_reason = should_require_manual_review(account)
    if manual_req:
        return StorySafetyDecision(
            allowed=False,
            reason_code=REASON_MANUAL_REVIEW_REQUIRED,
            human_reason=manual_reason or "Manual review required.",
            risk_level=RISK_WARMING,
            operator_action="Warm gradually; avoid bulk use",
        )

    # 8. Daily cap
    try:
        from src.core.warmup import is_daily_cap_exceeded
        cap_exceeded = is_daily_cap_exceeded(account)
    except Exception:
        cap_exceeded = False
    if cap_exceeded:
        return StorySafetyDecision(
            allowed=False,
            reason_code=REASON_DAILY_CAP,
            human_reason="Daily story cap exceeded.",
            next_allowed_at=utc_now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1),
            risk_level=RISK_SAFE,
            operator_action="Try tomorrow",
        )

    risk_level = get_account_risk_level(account)
    return StorySafetyDecision(
        allowed=True,
        reason_code="ok",
        human_reason="Eligible for story.",
        risk_level=risk_level,
        precheck_overrode_stale=precheck_overrode,
    )


def can_attempt_story(account: Any) -> tuple[bool, str, str]:
    """
    Returns (allowed, reason_code, human_reason).
    Convenience wrapper around get_story_safety_decision.
    """
    dec = get_story_safety_decision(account)
    return dec.allowed, dec.reason_code, dec.human_reason


def can_bulk_change_username(account: Any, changes_last_hour: int) -> tuple[bool, str]:
    """
    Returns (allowed, reason). Checks per-account + global hourly cap.
    """
    cfg = _get_settings()
    w = getattr(cfg, "warmup", None)
    max_per_hour = getattr(w, "max_username_changes_per_hour", 10) or 10
    if changes_last_hour >= max_per_hour:
        return False, f"Global limit: max {max_per_hour} username changes per hour."
    return True, ""


def can_bulk_change_photo(account: Any, changes_last_hour: int) -> tuple[bool, str]:
    """
    Returns (allowed, reason). Checks global hourly cap.
    """
    cfg = _get_settings()
    w = getattr(cfg, "warmup", None)
    max_per_hour = getattr(w, "max_profile_photo_changes_per_hour", 5) or 5
    if changes_last_hour >= max_per_hour:
        return False, f"Global limit: max {max_per_hour} profile photo changes per hour."
    return True, ""


def should_require_manual_review(account: Any) -> tuple[bool, str]:
    """
    Returns (required, reason). E.g. recently imported + no successful story + no profile history.
    """
    imported_at = _parse_dt(getattr(account, "imported_at", None))
    success = getattr(account, "successful_story_count", 0) or 0
    if success > 0:
        return False, ""
    if imported_at is None:
        return False, ""  # Legacy
    hours_since = (utc_now() - imported_at).total_seconds() / 3600
    if hours_since < 48:
        return True, "Imported recently with no successful story. Warm gradually."
    return False, ""


def log_story_exclusion(
    account_id: int,
    phone: Optional[str],
    requested_action: str,
    decision: StorySafetyDecision,
) -> None:
    """Structured log for forensics."""
    logger.info(
        "story_safety_excluded",
        account_id=account_id,
        phone=phone[:6] + "***" if phone and len(phone) > 6 else None,
        requested_action=requested_action,
        reason_code=decision.reason_code,
        human_reason=decision.human_reason[:100],
        risk_level=decision.risk_level,
        next_allowed_at=decision.next_allowed_at.isoformat() if decision.next_allowed_at else None,
    )
