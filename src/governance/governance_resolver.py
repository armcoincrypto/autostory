"""Unified governance policy resolution (visibility + metadata; does not widen execution)."""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from src.core.account_operational_state import compute_account_operational_state
from src.core.models import Account, AccountStatus
from src.governance.account_roles import get_db_roles, get_db_tags
from src.governance.constants import (
    AI_RESERVED,
    DISCOVERY_ALLOWED,
    LIVE_ALLOWED,
    MANUAL_ONLY,
    QUARANTINED,
    ROLE_BADGE_META,
    SCHEDULER_ALLOWED,
    STORY_ALLOWED,
    SYSTEM_PROTECTED,
)
from src.governance.fallback import fallback_blocked_reasons, fallback_roles_for_account


def _badge_list(roles: list[str], tags: list[str]) -> list[dict[str, str]]:
    badges: list[dict[str, str]] = []
    seen: set[str] = set()
    for role in roles:
        meta = ROLE_BADGE_META.get(role)
        if not meta or role in seen:
            continue
        seen.add(role)
        badges.append(
            {
                "kind": "role",
                "code": role,
                "label": meta["label"],
                "tone": meta["tone"],
                "title": meta["title"],
            }
        )
    for tag in tags[:6]:
        badges.append(
            {
                "kind": "tag",
                "code": tag,
                "label": tag[:12],
                "tone": "info",
                "title": f"Tag: {tag}",
            }
        )
    return badges


def resolve_account_governance(db: Session, account: Account) -> dict[str, Any]:
    """Normalize governance view for one account."""
    aid = int(account.id)
    db_roles = set(get_db_roles(db, aid))
    fallback_roles = fallback_roles_for_account(aid)
    roles = sorted(db_roles | fallback_roles)
    tags = get_db_tags(db, aid)
    op = compute_account_operational_state(db, account)

    protected = SYSTEM_PROTECTED in roles
    manual_only = MANUAL_ONLY in roles or QUARANTINED in roles
    quarantined = QUARANTINED in roles
    ai_reserved = AI_RESERVED in roles

    blocked_reasons: list[str] = []
    blocked_reasons.extend(fallback_blocked_reasons(aid))
    if quarantined:
        blocked_reasons.append("role:QUARANTINED")
    if manual_only and MANUAL_ONLY in roles:
        blocked_reasons.append("role:MANUAL_ONLY")
    if protected:
        blocked_reasons.append("role:SYSTEM_PROTECTED")
    if ai_reserved:
        blocked_reasons.append("role:AI_RESERVED")

    # Operational state mirrors existing runtime (unchanged behavior).
    status = str(getattr(getattr(account, "status", None), "value", getattr(account, "status", "")) or "").lower()
    if status != AccountStatus.ACTIVE.value:
        blocked_reasons.append(f"account_status:{status or 'unknown'}")

    health = (getattr(account, "health_status", None) or "").strip().lower()
    readiness = (op.get("readiness_status") or "").strip().upper()
    technical_ok = health == "alive" or readiness == "READY"

    # Preview eligibility mirrors current runtime gates (no widening).
    governance_blocks = protected or quarantined or ai_reserved or manual_only
    operational_story_ok = bool(
        technical_ok
        and status == AccountStatus.ACTIVE.value
        and op.get("tier") == "fleet"
        and not governance_blocks
    )
    eligible_story_runtime = operational_story_ok
    eligible_scheduler = bool(op.get("scheduler_eligible")) and not governance_blocks
    eligible_discovery = bool(op.get("discovery_eligible")) and not governance_blocks

    if STORY_ALLOWED not in roles and operational_story_ok:
        blocked_reasons.append("info:STORY_ALLOWED_role_not_assigned")
    if SCHEDULER_ALLOWED not in roles and op.get("scheduler_eligible"):
        blocked_reasons.append("info:SCHEDULER_ALLOWED_role_not_assigned")
    if governance_blocks:
        eligible_story_runtime = False
        eligible_scheduler = False
        eligible_discovery = False

    return {
        "account_id": aid,
        "roles": roles,
        "db_roles": sorted(db_roles),
        "fallback_roles": sorted(fallback_roles),
        "tags": tags,
        "badges": _badge_list(roles, tags),
        "protected": protected,
        "manual_only": manual_only,
        "quarantined": quarantined,
        "ai_reserved": ai_reserved,
        "eligible_story_runtime": eligible_story_runtime,
        "eligible_scheduler": eligible_scheduler,
        "eligible_discovery": eligible_discovery,
        "eligible_live_story": bool(LIVE_ALLOWED in roles and operational_story_ok and not governance_blocks),
        "governance_capability_roles": {
            "story": STORY_ALLOWED in roles,
            "scheduler": SCHEDULER_ALLOWED in roles,
            "discovery": DISCOVERY_ALLOWED in roles,
            "live": LIVE_ALLOWED in roles,
        },
        "blocked_reasons": sorted(set(blocked_reasons)),
        "requires_manual_override": bool(protected or manual_only or quarantined),
        "operational": {
            "scheduler_eligible": bool(op.get("scheduler_eligible")),
            "discovery_eligible": bool(op.get("discovery_eligible")),
            "tier": op.get("tier"),
            "readiness_status": op.get("readiness_status"),
            "technical_health_ok": technical_ok,
        },
    }


def can_use_for_story(db: Session, account: Account) -> bool:
    return bool(resolve_account_governance(db, account)["eligible_story_runtime"])


def can_use_for_scheduler(db: Session, account: Account) -> bool:
    return bool(resolve_account_governance(db, account)["eligible_scheduler"])


def can_use_for_discovery(db: Session, account: Account) -> bool:
    return bool(resolve_account_governance(db, account)["eligible_discovery"])


def requires_manual_override(db: Session, account: Account) -> bool:
    return bool(resolve_account_governance(db, account)["requires_manual_override"])


def build_execution_eligibility_preview(
    db: Session,
    *,
    account_ids: list[int] | None = None,
    module: str = "stories",
) -> dict[str, Any]:
    """Fleet-level eligibility preview (visibility only)."""
    module = (module or "stories").strip().lower()
    q = db.query(Account).order_by(Account.id.asc())
    if account_ids:
        q = q.filter(Account.id.in_([int(x) for x in account_ids]))
    accounts = q.all()

    buckets = {
        "eligible": 0,
        "protected": 0,
        "manual_only": 0,
        "quarantined": 0,
        "ai_reserved": 0,
        "missing_capability_role": 0,
        "operational_blocked": 0,
    }
    details: list[dict[str, Any]] = []

    for account in accounts:
        gov = resolve_account_governance(db, account)
        if gov["protected"]:
            buckets["protected"] += 1
        elif gov["quarantined"]:
            buckets["quarantined"] += 1
        elif gov["manual_only"]:
            buckets["manual_only"] += 1
        elif gov["ai_reserved"]:
            buckets["ai_reserved"] += 1

        if module == "scheduler":
            eligible = gov["eligible_scheduler"]
        elif module == "discovery":
            eligible = gov["eligible_discovery"]
        elif module == "live_story":
            eligible = gov["eligible_live_story"]
        else:
            eligible = gov["eligible_story_runtime"]

        if eligible:
            buckets["eligible"] += 1
        elif not gov["protected"] and not gov["quarantined"] and not gov["manual_only"] and not gov["ai_reserved"]:
            buckets["operational_blocked"] += 1

        details.append(
            {
                "account_id": gov["account_id"],
                "eligible": eligible,
                "roles": gov["roles"],
                "blocked_reasons": gov["blocked_reasons"],
                "requires_manual_override": gov["requires_manual_override"],
            }
        )

    return {
        "module": module,
        "total_accounts": len(accounts),
        "summary": buckets,
        "details": details,
        "visibility_only": True,
        "runtime_execution_unchanged": True,
    }
