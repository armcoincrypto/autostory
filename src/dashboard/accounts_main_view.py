"""P10.13 normal Accounts page projection.

This is an operator-facing, read-only page model. It avoids the recovery diagnostics
noise from Accounts v2 while keeping health, purpose, and module usability visible.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from src.ai_agent.account_allowlist import RESERVED_AI_AGENT_ACCOUNT_IDS
from src.core.account_operational_state import CONTROLLER_ACCOUNT_IDS, compute_account_operational_state
from src.core.models import Account
from src.dashboard.scheduler_mutations import scheduler_mutations_enabled
from src.recovery.p10_2a_readonly_recovery_analysis import GOVERNED_COHORT_IDS
from src.recovery.p10_8_global_active_job_hygiene import build_account_usability_fields
from src.governance.account_roles import list_pinned_account_ids
from src.governance.governance_resolver import build_execution_eligibility_preview
from src.stories.story_readiness_resolver import (
    build_story_readiness_preview,
    empty_readiness_preview,
    resolve_account_story_readiness,
)
from src.recovery.p9_83_governance_observability import PROTECTED_IDS, PURPOSE_HOLD_IDS
from src.scheduler.campaign_governance import campaign_execution_enabled

import structlog

logger = structlog.get_logger(__name__)

DEFAULT_GOVERNANCE_ROLES_AVAILABLE = [
    "STORY_ALLOWED",
    "DISCOVERY_ALLOWED",
    "SCHEDULER_ALLOWED",
    "LIVE_ALLOWED",
    "QUARANTINED",
]


def empty_eligibility_preview(module: str = "stories") -> dict[str, Any]:
    """Safe template fallback when governance preview cannot be built."""
    return {
        "module": module,
        "total_accounts": 0,
        "summary": {
            "eligible": 0,
            "protected": 0,
            "manual_only": 0,
            "quarantined": 0,
            "ai_reserved": 0,
            "operational_blocked": 0,
            "missing_capability_role": 0,
        },
        "details": [],
        "visibility_only": True,
        "runtime_execution_unchanged": True,
        "preview_unavailable": True,
    }


def build_eligibility_preview_safe(db: Session, *, module: str) -> dict[str, Any]:
    try:
        return build_execution_eligibility_preview(db, module=module)
    except Exception as exc:
        logger.warning(
            "accounts_eligibility_preview_failed",
            module=module,
            error=str(exc),
        )
        return empty_eligibility_preview(module)


def build_story_readiness_preview_safe(db: Session) -> dict[str, Any]:
    try:
        return build_story_readiness_preview(db, include_protected=True)
    except Exception as exc:
        logger.warning("accounts_story_readiness_preview_failed", error=str(exc))
        return empty_readiness_preview()


def _status_value(account: Account) -> str:
    status = getattr(account, "status", None)
    return status.value if hasattr(status, "value") else str(status or "")


def _label(account: Account) -> str:
    username = (account.username or "").strip()
    if username:
        return username if username.startswith("@") else f"@{username}"
    return (account.phone_number or "").strip() or f"account_{int(account.id)}"


def _badge(label: str, tone: str) -> dict[str, str]:
    return {"label": label, "tone": tone}


def _module_badges(account_id: int, op: dict[str, Any], usability: dict[str, Any]) -> dict[str, dict[str, str]]:
    aid = int(account_id)
    no_touch = aid in PROTECTED_IDS or aid in PURPOSE_HOLD_IDS or aid in CONTROLLER_ACCOUNT_IDS
    scheduler_ok = bool(op.get("scheduler_eligible"))
    discovery_ok = bool(op.get("discovery_eligible"))

    stories = _badge("See story columns", "info")
    discovery = _badge("Ready", "success") if discovery_ok and not no_touch else _badge("Blocked", "danger")
    if scheduler_ok and not no_touch:
        scheduler = _badge("Eligible", "warning" if not scheduler_mutations_enabled() else "success")
    else:
        scheduler = _badge("Blocked", "danger")

    if no_touch:
        campaigns = _badge("Blocked", "danger")
    elif aid in GOVERNED_COHORT_IDS or not campaign_execution_enabled():
        campaigns = _badge("Governance required", "warning")
    else:
        campaigns = _badge("Approved", "success")

    return {
        "stories": stories,
        "discovery": discovery,
        "scheduler": scheduler,
        "campaigns": campaigns,
    }


def _account_safety_state(account_id: int) -> dict[str, str]:
    aid = int(account_id)
    if aid in PURPOSE_HOLD_IDS:
        return _badge("HELD", "danger")
    if aid in CONTROLLER_ACCOUNT_IDS or aid in PROTECTED_IDS:
        return _badge("PROTECTED", "danger")
    if aid in RESERVED_AI_AGENT_ACCOUNT_IDS:
        return _badge("AI RESERVED", "reserved")
    return _badge("OPERATIONAL", "success")


def build_accounts_main_context(db: Session) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    pinned_ids = set(list_pinned_account_ids(db))
    for account in db.query(Account).order_by(Account.id.asc()).all():
        aid = int(account.id)
        op = compute_account_operational_state(db, account)
        usability = build_account_usability_fields(db, aid)
        modules = _module_badges(aid, op, usability)
        health = usability.get("technical_health_label") or "unknown"
        try:
            story = resolve_account_story_readiness(db, account)
        except Exception as exc:
            logger.warning("account_story_readiness_failed", account_id=aid, error=str(exc))
            story = {
                "labels": {
                    "governance": "Unknown",
                    "governance_tone": "info",
                    "story_auth": "Unknown",
                    "story_auth_tone": "info",
                    "runtime": "Unknown",
                    "runtime_tone": "info",
                    "primary": "Unavailable",
                    "severity": "warning",
                },
                "governance": {"badges": [], "blocked_reasons": []},
                "runtime": {"blockers": ["readiness_unavailable"]},
                "governance_story_eligible": False,
                "runtime_story_ready": False,
                "needs_auth_probe": False,
            }
        gov_badges = []
        for key, label_key, tone_key in (
            ("governance", "governance", "governance_tone"),
            ("story_auth", "story_auth", "story_auth_tone"),
            ("runtime", "runtime", "runtime_tone"),
        ):
            gov_badges.append(
                {
                    "label": story["labels"][label_key],
                    "tone": story["labels"][tone_key],
                    "title": key,
                }
            )
        rows.append(
            {
                "id": aid,
                "label": _label(account),
                "phone": account.phone_number or "",
                "username": account.username or "",
                "status": _status_value(account) or "unknown",
                "purpose": (account.purpose or "both").strip().lower() or "both",
                "scheduler_eligible": bool(op.get("scheduler_eligible")),
                "discovery_eligible": bool(op.get("discovery_eligible")),
                "technical_health": "OK" if health == "ok" else health.replace("_", " ").title(),
                "technical_tone": "success" if health == "ok" else "warning",
                "modules": modules,
                "safety_state": _account_safety_state(aid),
                "story_readiness": story,
                "story_labels": story["labels"],
                "governance_badges": gov_badges,
                "governance_blocked_reasons": story.get("governance", {}).get("blocked_reasons", [])
                + story.get("runtime", {}).get("blockers", []),
                "requires_manual_override": bool(story.get("governance", {}).get("protected")),
                "governance_story_eligible": bool(story.get("governance_story_eligible")),
                "runtime_story_ready": bool(story.get("runtime_story_ready")),
                "needs_auth_probe": bool(story.get("needs_auth_probe")),
                "pinned": aid in pinned_ids,
                "active_job_count": int(usability.get("active_job_count") or 0),
            }
        )

    rows.sort(key=lambda r: (not r["pinned"], r["id"]))
    eligibility = build_eligibility_preview_safe(db, module="stories")
    scheduler_preview = build_eligibility_preview_safe(db, module="scheduler")
    discovery_preview = build_eligibility_preview_safe(db, module="discovery")
    story_readiness = build_story_readiness_preview_safe(db)

    return {
        "accounts": rows,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "total_accounts": len(rows),
        "operational_count": sum(1 for r in rows if r["safety_state"]["label"] == "OPERATIONAL"),
        "scheduler_locked": not scheduler_mutations_enabled(),
        "campaigns_locked": not campaign_execution_enabled(),
        "pinned_account_ids": sorted(pinned_ids),
        "eligibility_preview": eligibility,
        "eligibility_scheduler": scheduler_preview,
        "eligibility_discovery": discovery_preview,
        "story_readiness_preview": story_readiness,
        "governance_roles_available": [
            "STORY_ALLOWED",
            "DISCOVERY_ALLOWED",
            "SCHEDULER_ALLOWED",
            "LIVE_ALLOWED",
            "QUARANTINED",
        ],
    }
