"""P10.13 normal Accounts page projection.

Operator-facing, read-only page model. Primary Story status comes from the
canonical fleet readiness matrix via ``operator_account_presentation``.
Diagnostics remain available in an expandable drawer only.
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
from src.stories.fleet_readiness_matrix import load_latest_matrix, matrix_freshness
from src.stories.operator_account_presentation import build_operator_account_views
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


def _status_value(account: Account) -> str:
    status = getattr(account, "status", None)
    return status.value if hasattr(status, "value") else str(status or "")


def _label(account: Account) -> str:
    phone = (account.phone_number or "").strip()
    if phone:
        return phone
    username = (account.username or "").strip()
    if username:
        return username if username.startswith("@") else f"@{username}"
    return f"account_{int(account.id)}"


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
    pinned_ids = set(list_pinned_account_ids(db))
    matrix = load_latest_matrix()
    freshness = matrix_freshness(matrix)
    operator_views = build_operator_account_views(matrix, freshness=freshness)
    mapped_by_id = operator_views["accounts_by_id"]

    rows: list[dict[str, Any]] = []
    for account in db.query(Account).order_by(Account.id.asc()).all():
        aid = int(account.id)
        op = compute_account_operational_state(db, account)
        usability = build_account_usability_fields(db, aid)
        modules = _module_badges(aid, op, usability)
        health = usability.get("technical_health_label") or "unknown"
        presentation = mapped_by_id.get(aid)
        if presentation is None:
            # Account present in DB but missing from matrix — force check required.
            presentation = {
                "account_id": aid,
                "display_status": "CHECK_REQUIRED",
                "status_label": "Check required",
                "status_detail": "Run authorization probe",
                "authorization_label": "Unknown",
                "role_label": "Unknown",
                "required_action": "Run authorization probe",
                "severity": "warning",
                "diagnostic_reason": "missing_from_canonical_matrix",
                "row_action": "view_details",
                "tooltip": None,
                "last_checked": None,
                "filter_group": "needs_attention",
                "canonical": {},
            }

        label = _label(account)
        rows.append(
            {
                "id": aid,
                "label": label,
                "phone": account.phone_number or "",
                "username": account.username or "",
                "telegram_user_id": getattr(account, "user_id", None),
                "status": _status_value(account) or "unknown",
                "purpose": (account.purpose or "both").strip().lower() or "both",
                "scheduler_eligible": bool(op.get("scheduler_eligible")),
                "discovery_eligible": bool(op.get("discovery_eligible")),
                "technical_health": "OK" if health == "ok" else health.replace("_", " ").title(),
                "technical_tone": "success" if health == "ok" else "warning",
                "modules": modules,
                "safety_state": _account_safety_state(aid),
                "pinned": aid in pinned_ids,
                "active_job_count": int(usability.get("active_job_count") or 0),
                # Primary operator presentation (canonical mapper).
                "display_status": presentation["display_status"],
                "status_label": presentation["status_label"],
                "status_detail": presentation["status_detail"],
                "authorization_label": presentation["authorization_label"],
                "role_label": presentation["role_label"],
                "required_action": presentation["required_action"],
                "severity": presentation["severity"],
                "diagnostic_reason": presentation["diagnostic_reason"],
                "row_action": presentation["row_action"],
                "tooltip": presentation.get("tooltip"),
                "last_checked": presentation.get("last_checked"),
                "filter_group": presentation.get("filter_group") or "all",
                "canonical": presentation.get("canonical") or {},
            }
        )

    rows.sort(key=lambda r: (not r["pinned"], r["id"]))
    eligibility = build_eligibility_preview_safe(db, module="stories")
    scheduler_preview = build_eligibility_preview_safe(db, module="scheduler")
    discovery_preview = build_eligibility_preview_safe(db, module="discovery")
    summary = operator_views["summary"]

    return {
        "accounts": rows,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "total_accounts": summary["total_accounts"] or len(rows),
        "operator_summary": summary,
        "freshness_compact": summary["freshness_compact"],
        "default_filter": summary["default_filter"],
        "scheduler_locked": not scheduler_mutations_enabled(),
        "campaigns_locked": not campaign_execution_enabled(),
        "automation_locked": True,
        "pinned_account_ids": sorted(pinned_ids),
        "eligibility_preview": eligibility,
        "eligibility_scheduler": scheduler_preview,
        "eligibility_discovery": discovery_preview,
        "governance_roles_available": list(DEFAULT_GOVERNANCE_ROLES_AVAILABLE),
        "canonical_source": operator_views.get("canonical_source"),
    }
