"""
Canonical Telethon session paths and read-only story/session projections for API + safety policy.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import structlog

logger = structlog.get_logger(__name__)

_ACCOUNT_SESSION_RE = re.compile(r"^account_(\d+)\.session$", re.IGNORECASE)


def get_sessions_dir() -> Path:
    from config.settings import settings

    p = Path(settings.storage.sessions_dir)
    return p.expanduser().resolve()


def get_canonical_session_path(account_id: int) -> Path:
    """Preferred session file: ``{sessions_dir}/account_{id}.session``."""
    return get_sessions_dir() / f"account_{int(account_id)}.session"


def get_existing_canonical_account_ids() -> set[int]:
    """IDs that have an ``account_<id>.session`` file on disk (fast batch checks)."""
    ids: set[int] = set()
    d = get_sessions_dir()
    if not d.is_dir():
        return ids
    for p in d.iterdir():
        if not p.is_file():
            continue
        m = _ACCOUNT_SESSION_RE.match(p.name)
        if m:
            try:
                ids.add(int(m.group(1)))
            except ValueError:
                continue
    return ids


def account_has_canonical_session(
    account: Any,
    _canonical_exists: set[int] | None = None,
) -> bool:
    """
    True when a usable session file exists: canonical ``account_<id>.session`` or existing ``session_path``.
    When ``_canonical_exists`` is provided (from ``get_existing_canonical_account_ids``), uses membership
    plus a fallback for DB ``session_path`` files not under the canonical glob pattern.
    """
    aid = getattr(account, "id", None)
    if aid is None:
        return False
    sp = getattr(account, "session_path", None)

    def _path_ok(p: Optional[str]) -> bool:
        if not p or not isinstance(p, str):
            return False
        try:
            return Path(p).expanduser().is_file()
        except OSError:
            return False

    if _canonical_exists is not None:
        if int(aid) in _canonical_exists:
            return True
        if _path_ok(sp):
            return True
        return False

    if get_canonical_session_path(int(aid)).is_file():
        return True
    if _path_ok(sp):
        return True
    return False


def get_session_readiness(
    account: Any,
    _canonical_exists: set[int] | None = None,
) -> str:
    """
    UI/session layer hint: canonical_ok | missing_canonical | needs_reimport
    (aligned with ``_accounts_summary_from_result``).
    """
    if account_has_canonical_session(account, _canonical_exists=_canonical_exists):
        return "canonical_ok"
    sp = getattr(account, "session_path", None)
    if sp and str(sp).strip():
        return "needs_reimport"
    return "missing_canonical"


def classify_account_readiness(account: Any) -> str:
    """
    Audit classification: session_ready, missing_canonical_session, needs_reimport, auth_required,
    frozen_story, story_rate_limited, restricted, other.
    """
    st_obj = getattr(account, "status", None)
    st = (st_obj.value if hasattr(st_obj, "value") else str(st_obj or "")) or ""
    if st == "auth_required":
        return "auth_required"

    if not account_has_canonical_session(account):
        sp = getattr(account, "session_path", None)
        if sp and str(sp).strip():
            return "needs_reimport"
        return "missing_canonical_session"

    ss = (getattr(account, "story_status", None) or "").lower()
    if ss == "rate_limited":
        return "story_rate_limited"
    if ss == "restricted":
        return "restricted"
    if ss == "frozen":
        return "frozen_story"

    hs = getattr(account, "health_status", None) or ""
    if hs == "frozen":
        return "frozen_story"

    return "session_ready"


def recommended_action(classification: str) -> str:
    return {
        "session_ready": "OK — verify story safety before bulk sends",
        "missing_canonical_session": "Import canonical session (account_<id>.session)",
        "needs_reimport": "Re-import session; file missing at stored session_path",
        "auth_required": "Run health check or re-auth",
        "frozen_story": "Resolve Telegram story restriction",
        "story_rate_limited": "Wait for story rate limit to clear",
        "restricted": "Resolve Telegram account restriction",
        "other": "Review account",
    }.get(classification, "Review account")


def _parse_datetime_safe(val: Any) -> Optional[datetime]:
    if val is None:
        return None
    if hasattr(val, "year"):
        if getattr(val, "tzinfo", None) is not None:
            return val.astimezone(timezone.utc).replace(tzinfo=None)
        return val
    try:
        raw = str(val).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        return None


def _format_datetime_op(d: datetime) -> str:
    return d.strftime("%Y-%m-%d %H:%M:%S")


def get_story_availability(account: Any, _canonical_exists: set[int] | None = None) -> dict:
    """
    Return normalized story availability for UI/API.
    Strict projection of ``get_story_safety_decision``; must stay aligned with safety policy.
    """
    try:
        from src.core.safety_policy import get_story_safety_decision
        from src.core.safety_policy import (
            REASON_NO_SESSION,
            REASON_AUTH_REQUIRED,
            REASON_WARMUP_PENDING,
            REASON_STORY_PRECHECK_FAILED,
            REASON_STORY_RATE_LIMITED,
            REASON_STORY_FROZEN,
            REASON_STORY_RESTRICTED,
            REASON_STORY_BLOCKED,
            REASON_STORY_TELEGRAM_DENIED,
            REASON_MANUAL_REVIEW_REQUIRED,
            REASON_COOLDOWN,
            REASON_DAILY_CAP,
            REASON_TOO_MANY_STORY_ATTEMPTS,
        )
        decision = get_story_safety_decision(
            account, requested_action="story_publish", _canonical_exists=_canonical_exists
        )
    except Exception:
        logger.exception("get_story_availability safety decision failed")
        return {
            "story_ui_status": "unknown",
            "story_available_at": None,
            "story_available_label": "—",
            "story_reason": "—",
            "is_story_ready": False,
            "story_precheck_stale": True,
        }

    precheck_checked_at = _parse_datetime_safe(getattr(account, "story_precheck_checked_at", None))
    precheck_ttl_post = 15
    try:
        from config.settings import settings

        w = getattr(settings, "warmup", None)
        precheck_ttl_post = getattr(w, "precheck_ttl_post_minutes", 15) or 15
    except Exception:
        pass

    now = datetime.utcnow()
    precheck_stale = precheck_checked_at is None or (
        (now - precheck_checked_at).total_seconds() > (precheck_ttl_post * 60)
    )

    reason = (getattr(account, "story_status_reason", None) or "").strip() or "—"
    human = decision.human_reason or "—"
    next_at = decision.next_allowed_at

    def _avail_at():
        if next_at:
            return next_at.isoformat() if hasattr(next_at, "isoformat") else str(next_at)
        return None

    def _avail_label():
        if next_at and next_at > now:
            return _format_datetime_op(next_at)
        return "—"

    if decision.allowed:
        return {
            "story_ui_status": "ready",
            "story_available_at": None,
            "story_available_label": "Now",
            "story_reason": reason,
            "is_story_ready": True,
            "story_precheck_stale": precheck_stale,
        }

    code = decision.reason_code
    if code == REASON_STORY_FROZEN:
        return {
            "story_ui_status": "frozen",
            "story_available_at": None,
            "story_available_label": "Unknown",
            "story_reason": human,
            "is_story_ready": False,
            "story_precheck_stale": precheck_stale,
        }
    if code == REASON_STORY_BLOCKED:
        return {
            "story_ui_status": "blocked",
            "story_available_at": None,
            "story_available_label": "Unknown",
            "story_reason": human,
            "is_story_ready": False,
            "story_precheck_stale": precheck_stale,
        }
    if code == REASON_STORY_TELEGRAM_DENIED:
        return {
            "story_ui_status": "telegram_denied",
            "story_available_at": None,
            "story_available_label": "Unknown",
            "story_reason": human,
            "is_story_ready": False,
            "story_precheck_stale": precheck_stale,
        }
    if code == REASON_STORY_RESTRICTED:
        return {
            "story_ui_status": "restricted",
            "story_available_at": None,
            "story_available_label": "Unknown",
            "story_reason": human,
            "is_story_ready": False,
            "story_precheck_stale": precheck_stale,
        }
    if code == REASON_STORY_RATE_LIMITED:
        return {
            "story_ui_status": "rate_limited",
            "story_available_at": _avail_at(),
            "story_available_label": _avail_label(),
            "story_reason": human,
            "is_story_ready": False,
            "story_precheck_stale": precheck_stale,
        }

    if precheck_stale:
        never = precheck_checked_at is None
        return {
            "story_ui_status": "needs_precheck",
            "story_available_at": None,
            "story_available_label": "Not checked" if never else "Precheck stale",
            "story_reason": human,
            "is_story_ready": False,
            "story_precheck_stale": True,
        }

    if code == REASON_NO_SESSION or code == REASON_AUTH_REQUIRED:
        return {
            "story_ui_status": "unknown",
            "story_available_at": None,
            "story_available_label": "—",
            "story_reason": human,
            "is_story_ready": False,
            "story_precheck_stale": precheck_stale,
        }
    if code == REASON_STORY_PRECHECK_FAILED:
        return {
            "story_ui_status": "unknown",
            "story_available_at": None,
            "story_available_label": "Precheck required",
            "story_reason": human,
            "is_story_ready": False,
            "story_precheck_stale": precheck_stale,
        }
    if code == REASON_WARMUP_PENDING:
        return {
            "story_ui_status": "warmup_hold",
            "story_available_at": _avail_at(),
            "story_available_label": _avail_label() if next_at else "Warmup Hold",
            "story_reason": human,
            "is_story_ready": False,
            "story_precheck_stale": precheck_stale,
        }
    if code == REASON_TOO_MANY_STORY_ATTEMPTS or code == REASON_DAILY_CAP:
        return {
            "story_ui_status": "unknown",
            "story_available_at": _avail_at(),
            "story_available_label": _avail_label() if next_at else "Try tomorrow",
            "story_reason": human,
            "is_story_ready": False,
            "story_precheck_stale": precheck_stale,
        }
    if code == REASON_COOLDOWN:
        return {
            "story_ui_status": "unknown",
            "story_available_at": _avail_at(),
            "story_available_label": _avail_label(),
            "story_reason": human,
            "is_story_ready": False,
            "story_precheck_stale": precheck_stale,
        }
    if code == REASON_MANUAL_REVIEW_REQUIRED:
        return {
            "story_ui_status": "unknown",
            "story_available_at": None,
            "story_available_label": "—",
            "story_reason": human,
            "is_story_ready": False,
            "story_precheck_stale": precheck_stale,
        }

    return {
        "story_ui_status": "unknown",
        "story_available_at": _avail_at(),
        "story_available_label": "—",
        "story_reason": human,
        "is_story_ready": False,
        "story_precheck_stale": precheck_stale,
    }
