"""
P10.9B — Module usability labels for Accounts v2 dashboard cards.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session

from src.ai_agent.account_allowlist import RESERVED_AI_AGENT_ACCOUNT_IDS
from src.core.account_operational_state import CONTROLLER_ACCOUNT_IDS
from src.core.models import Account
from src.dashboard.scheduler_mutations import scheduler_mutations_enabled
from src.recovery.p10_2a_readonly_recovery_analysis import GOVERNED_COHORT_IDS
from src.recovery.p10_5_final_fleet_gap_audit import P10_4_RESTORED_COHORT
from src.recovery.p10_8_global_active_job_hygiene import build_account_usability_fields
from src.recovery.p9_83_governance_observability import PROTECTED_IDS, PURPOSE_HOLD_IDS
from src.scheduler.campaign_governance import campaign_execution_enabled

_BADGE = {
    "ready": "mod-badge-ready",
    "blocked": "mod-badge-blocked",
    "gated": "mod-badge-gated",
    "reserved": "mod-badge-reserved",
    "preview": "mod-badge-preview",
}


def _badge(allowed: bool, *, gated: bool = False, reserved: bool = False) -> tuple[str, str]:
    if reserved:
        return "Reserved only", _BADGE["reserved"]
    if gated:
        return "Globally locked", _BADGE["gated"]
    if allowed:
        return "Ready", _BADGE["ready"]
    return "Blocked", _BADGE["blocked"]


def activation_status_label(
    db: Session,
    account_id: int,
    *,
    usability: Optional[dict[str, Any]] = None,
    meta: Optional[dict[str, Any]] = None,
) -> dict[str, str]:
    aid = int(account_id)
    u = usability or build_account_usability_fields(db, aid)
    if aid in PROTECTED_IDS or aid in PURPOSE_HOLD_IDS:
        return {
            "label": "No-touch account",
            "css": "activation-no-touch",
        }
    if aid in RESERVED_AI_AGENT_ACCOUNT_IDS:
        return {"label": "AI-agent reserved tier", "css": "activation-reserved"}
    purpose = (u.get("purpose_raw") or "").strip().lower()
    if purpose in ("both", "messaging", "autostory") and u.get("technical_health_label") == "ok":
        sched = meta.get("scheduler_eligible") if meta else None
        if sched is None:
            acc = db.get(Account, aid)
            if acc:
                from src.clients.manager_v2 import get_account_metadata

                sched = get_account_metadata(db, aid).get("scheduler_eligible")
        return {
            "label": "Operationally enabled",
            "css": "activation-enabled",
            "detail": "purpose=both · scheduler eligible · discovery eligible · campaign not approved",
        }
    if aid in P10_4_RESTORED_COHORT and u.get("technical_health_label") == "ok":
        return {
            "label": "Technically healthy, disabled",
            "css": "activation-disabled-ready",
            "detail": "Can be activated in controlled batch",
        }
    return {"label": "Needs review", "css": "activation-review"}


def build_module_usability_block(
    db: Session,
    account_id: int,
    *,
    meta: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    aid = int(account_id)
    from src.recovery.p10_9_live_dashboard_verification import module_eligibility_for_account

    mods = module_eligibility_for_account(db, aid)
    global_lock = scheduler_mutations_enabled()
    tier_controller = aid in CONTROLLER_ACCOUNT_IDS or aid in (206, 207)

    stories = mods.get("stories") or {}
    discovery = mods.get("discovery") or {}
    scheduler = mods.get("scheduler") or {}
    campaigns = mods.get("campaigns") or {}
    ai = mods.get("ai_agent") or {}
    dexpert = mods.get("dexpert") or {}

    sched_allowed = bool(scheduler.get("allowed"))
    sched_note = ""
    if sched_allowed and global_lock:
        sched_label, sched_css = "Eligible", _BADGE["gated"]
        sched_note = "Globally locked"
    else:
        sched_label, sched_css = _badge(sched_allowed)

    camp_allowed = bool(campaigns.get("allowed"))
    if aid in GOVERNED_COHORT_IDS:
        camp_label = "Governance required" if not camp_allowed else "Approved"
        camp_css = _BADGE["gated"] if not camp_allowed else _BADGE["ready"]
    else:
        camp_label, camp_css = _badge(camp_allowed)

    ai_reserved = aid in RESERVED_AI_AGENT_ACCOUNT_IDS or aid in PROTECTED_IDS
    ai_label, ai_css = (
        ("Assigned" if ai_reserved else "Not assigned", _BADGE["reserved" if ai_reserved else "blocked"])
    )

    dex_label, dex_css = (
        ("Controller only" if tier_controller else "Not assigned", _BADGE["reserved" if tier_controller else "blocked"])
    )

    return {
        "activation": activation_status_label(db, aid, meta=meta),
        "modules": [
            {"name": "Stories", "label": _badge(stories.get("allowed"))[0], "css": _badge(stories.get("allowed"))[1]},
            {"name": "Discovery", "label": _badge(discovery.get("allowed"))[0], "css": _badge(discovery.get("allowed"))[1]},
            {"name": "Scheduler", "label": sched_label, "css": sched_css, "note": sched_note if sched_allowed and global_lock else ""},
            {"name": "Campaigns", "label": camp_label, "css": camp_css},
            {"name": "AI Agent", "label": ai_label, "css": ai_css},
            {"name": "Dexpert", "label": dex_label, "css": dex_css},
        ],
        "safety_copy": [
            "Technical health OK does not mean the account can send.",
            "Scheduler eligible does not bypass global scheduler lock.",
            "Campaigns require governance approval.",
        ],
    }
