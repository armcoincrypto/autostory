"""
STORYFLEET Profile Action Policy - Eligibility for bulk username/photo changes.
Single source of truth for operator-profile actions. Default: deny.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import structlog

logger = structlog.get_logger(__name__)

REASON_NO_SESSION = "no_session"
REASON_AUTH_REQUIRED = "auth_required"
REASON_FROZEN = "frozen"
REASON_RESTRICTED = "restricted"
REASON_MANUAL_REVIEW_REQUIRED = "manual_review_required"
REASON_WARMING_BLOCKED = "warming_blocked"
REASON_COOLDOWN = "per_account_cooldown"


@dataclass
class ProfileActionDecision:
    """Result of get_profile_action_eligibility."""
    allowed: bool
    reason_code: str
    human_reason: str


def _get_settings() -> Any:
    try:
        from config.settings import settings
        return settings
    except Exception:
        return None


def get_profile_action_eligibility(
    account: Any,
    action_type: str,  # "bulk_username" | "bulk_photo"
    allow_warming_override: bool = False,
    override_reason: Optional[str] = None,
) -> ProfileActionDecision:
    """
    Single source of truth for bulk profile action eligibility.
    Checks: session, status, frozen/restricted, manual_review, warming, per-account cooldown.
    Default: deny. Warming accounts blocked unless allow_warming_override with reason.
    """
    try:
        from src.core.session_paths import account_has_canonical_session
        has_session = account_has_canonical_session(account)
    except Exception:
        has_session = False
    if not has_session:
        return ProfileActionDecision(
            allowed=False,
            reason_code=REASON_NO_SESSION,
            human_reason="No session file. Re-import via TDATA.",
        )

    st = getattr(account, "status", None)
    st_val = (st.value if hasattr(st, "value") else str(st or "")) or ""
    if st_val not in ("active",):
        return ProfileActionDecision(
            allowed=False,
            reason_code=REASON_AUTH_REQUIRED,
            human_reason=f"Account status={st_val}. Not safe for bulk changes.",
        )

    hs = getattr(account, "health_status", None)
    if hs in ("frozen", "restricted", "banned", "deleted"):
        return ProfileActionDecision(
            allowed=False,
            reason_code=REASON_FROZEN if hs == "frozen" else REASON_RESTRICTED,
            human_reason=f"Account {hs}. Avoid bulk profile changes.",
        )

    ss = getattr(account, "story_status", None) or ""
    if ss in ("frozen", "restricted"):
        return ProfileActionDecision(
            allowed=False,
            reason_code=REASON_FROZEN if ss == "frozen" else REASON_RESTRICTED,
            human_reason=f"Story status={ss}. Avoid bulk profile changes.",
        )

    if getattr(account, "manual_review_required", False):
        return ProfileActionDecision(
            allowed=False,
            reason_code=REASON_MANUAL_REVIEW_REQUIRED,
            human_reason="Manual review required. Clear before bulk changes.",
        )

    ws = getattr(account, "warmup_status", None) or ""
    try:
        from src.core.warmup import get_warmup_status
        ws = get_warmup_status(account)
    except Exception:
        pass
    if ws in ("new", "warming"):
        if not allow_warming_override or not (override_reason or "").strip():
            return ProfileActionDecision(
                allowed=False,
                reason_code=REASON_WARMING_BLOCKED,
                human_reason="Fresh/warming account. Add allow_warming_override with reason to bypass.",
            )

    event_type = "username_changed" if action_type == "bulk_username" else "profile_photo_changed"
    try:
        from src.core.risk_events import count_events_for_account_since
        from config.settings import settings
        w = getattr(settings, "warmup", None)
        hours = 6.0
        if action_type == "bulk_username":
            hours = float(getattr(w, "username_change_cooldown_hours", 6) or 6)
        else:
            hours = float(getattr(w, "profile_photo_change_cooldown_hours", 12) or 12)
        n = count_events_for_account_since(account.id, event_type, hours)
        if n > 0:
            return ProfileActionDecision(
                allowed=False,
                reason_code=REASON_COOLDOWN,
                human_reason=f"Per-account cooldown: {n} change(s) in last {hours:.0f}h.",
            )
    except Exception:
        pass

    return ProfileActionDecision(allowed=True, reason_code="ok", human_reason="Eligible.")


def _get_eligible_accounts_internal(
    action_type: str,
    max_accounts: int,
    max_warming: int,
    allow_warming_override: bool,
    override_reason: Optional[str],
) -> tuple[list[Any], list[dict]]:
    """
    Internal: returns (eligible_accounts, skipped_list).
    eligible_accounts are Account objects; warming accounts included via override
    have _profile_override_used=True set.
    """
    from src.core.database import get_db_context
    from src.core.models import Account
    from src.core.session_paths import account_has_canonical_session

    with get_db_context() as db:
        accounts = db.query(Account).order_by(Account.id).all()
    accounts = [a for a in accounts if account_has_canonical_session(a)]

    eligible: list[Any] = []
    skipped: list[dict] = []
    warming_count = 0

    for a in accounts:
        dec = get_profile_action_eligibility(
            a, action_type,
            allow_warming_override=allow_warming_override,
            override_reason=override_reason,
        )
        if dec.allowed:
            ws = getattr(a, "warmup_status", None) or ""
            try:
                from src.core.warmup import get_warmup_status
                ws = get_warmup_status(a)
            except Exception:
                pass
            is_warming = ws in ("new", "warming")
            if is_warming and warming_count >= max_warming:
                skipped.append({
                    "id": a.id,
                    "reason_code": "warming_cap",
                    "human_reason": f"Max {max_warming} warming accounts per batch.",
                })
                logger.info("profile_action_skipped", account_id=a.id, reason="warming_cap")
                continue
            if is_warming:
                warming_count += 1
            setattr(a, "_profile_override_used", is_warming)
            eligible.append(a)
            if len(eligible) >= max_accounts:
                break
        else:
            skipped.append({
                "id": a.id,
                "reason_code": dec.reason_code,
                "human_reason": dec.human_reason,
            })
            logger.info(
                "profile_action_skipped",
                account_id=a.id,
                reason_code=dec.reason_code,
                human_reason=dec.human_reason[:80],
            )

    return eligible, skipped


def get_profile_action_eligible_accounts(
    action_type: str,
    allow_warming_override: bool = False,
    override_reason: Optional[str] = None,
) -> tuple[list[Any], list[dict]]:
    """
    Single entry point for bulk profile actions. Returns (eligible_accounts, skipped).
    Uses settings: max_bulk_username_batch/max_bulk_photo_batch, max_warming_accounts_per_bulk.
    Batch cap is applied by the caller (routes); this returns all eligible up to a large limit.
    """
    try:
        from config.settings import settings
        w = getattr(settings, "warmup", None)
    except Exception:
        w = None
    max_batch = 10
    max_warming = 0
    if action_type == "bulk_username":
        max_batch = int(getattr(w, "max_bulk_username_batch", 5) or 5)
        max_warming = int(getattr(w, "max_warming_accounts_per_bulk", 0) or 0)
    else:
        max_batch = int(getattr(w, "max_bulk_photo_batch", 3) or 3)
        max_warming = int(getattr(w, "max_warming_accounts_per_bulk", 0) or 0)
    # When override is used, allow up to max_warming warming accounts
    effective_max_warming = max_warming if allow_warming_override else 0
    return _get_eligible_accounts_internal(
        action_type=action_type,
        max_accounts=500,
        max_warming=effective_max_warming,
        allow_warming_override=allow_warming_override,
        override_reason=override_reason,
    )
