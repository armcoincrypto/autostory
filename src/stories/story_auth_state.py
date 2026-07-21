"""P10.22B persisted story authorization state (read-only hydration)."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from src.core.models import Account
from src.stories.rotation_audit import FRESH_STORY_AUTH_TTL_MINUTES, iso, parse_dt, utcnow


def resolve_story_auth_state(account: Account, *, now=None) -> dict[str, Any]:
    """Single source of truth for persisted story precheck / fresh auth display."""
    now = now or utcnow()
    status = (getattr(account, "story_precheck_status", None) or "").strip().lower()
    reason = (getattr(account, "story_precheck_reason", None) or "").strip() or None
    checked_at = parse_dt(getattr(account, "story_precheck_checked_at", None))
    blocked_until = parse_dt(getattr(account, "story_blocked_until", None))
    checked_at_iso = iso(getattr(account, "story_precheck_checked_at", None))
    blocked_until_iso = iso(getattr(account, "story_blocked_until", None))

    if blocked_until is not None and blocked_until > now:
        return {
            "state": "blocked",
            "fresh": False,
            "label": "Auth blocked",
            "tone": "danger",
            "checked_at": checked_at_iso,
            "blocked_until": blocked_until_iso,
            "status": status or None,
            "reason": reason,
            "blockers": ["story_auth_blocked_until_future"],
        }

    if status == "allowed" and checked_at is not None:
        age = now - checked_at
        if age <= timedelta(minutes=FRESH_STORY_AUTH_TTL_MINUTES):
            return {
                "state": "ok",
                "fresh": True,
                "label": "Auth OK",
                "tone": "success",
                "checked_at": checked_at_iso,
                "blocked_until": blocked_until_iso,
                "status": status,
                "reason": reason,
                "blockers": [],
            }
        return {
            "state": "fresh_auth_stale",
            "fresh": False,
            "label": "Auth stale",
            "tone": "warning",
            "checked_at": checked_at_iso,
            "blocked_until": blocked_until_iso,
            "status": status,
            "reason": reason,
            "blockers": ["fresh_story_auth_stale"],
        }

    if status and status != "allowed":
        return {
            "state": "failed",
            "fresh": False,
            "label": "Auth failed",
            "tone": "danger",
            "checked_at": checked_at_iso,
            "blocked_until": blocked_until_iso,
            "status": status,
            "reason": reason,
            "blockers": [f"story_precheck_not_allowed:{status}"],
        }

    return {
        "state": "fresh_auth_required",
        "fresh": False,
        "label": "Auth required",
        "tone": "warning",
        "checked_at": checked_at_iso,
        "blocked_until": blocked_until_iso,
        "status": status or None,
        "reason": reason,
        "blockers": ["fresh_story_auth_required"],
    }


def story_auth_is_fresh(account: Account, *, now=None) -> bool:
    """Execution gate: persisted allowed precheck within freshness TTL."""
    return bool(resolve_story_auth_state(account, now=now).get("fresh"))
