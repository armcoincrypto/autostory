"""P10.22 unified story readiness truth (read-only; no execution widening)."""
from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy.orm import Session

from src.core.account_protection import PROTECTED_IDS, PURPOSE_HOLD_IDS
from src.core.models import Account
from src.governance.governance_resolver import resolve_account_governance
from src.stories.story_auth_state import resolve_story_auth_state
from src.stories.rotation_audit import evaluate_account_story_runtime, story_purpose_compatible

logger = structlog.get_logger(__name__)


def _display_for_account(account: Account) -> str:
    phone = (getattr(account, "phone_number", None) or "").strip()
    if phone:
        return phone
    username = (getattr(account, "username", None) or "").strip()
    if username:
        return username if username.startswith("@") else f"@{username}"
    return f"#{int(account.id)}"


def _auth_layer(account: Account, runtime_row: dict[str, Any]) -> dict[str, Any]:
    """Hydrate auth from persisted Account precheck fields (not inferred blockers alone)."""
    auth = resolve_story_auth_state(account)
    if not auth.get("fresh") and not auth.get("blockers"):
        live_only = list(runtime_row.get("live_only_blockers") or [])
        hard = list(runtime_row.get("blockers") or [])
        if any("fresh_story_auth_stale" in b for b in live_only):
            auth = {**auth, "state": "fresh_auth_stale", "label": "Auth stale", "tone": "warning", "blockers": ["fresh_story_auth_stale"]}
        elif any("fresh_story_auth_required" in b for b in live_only):
            auth = {**auth, "state": "fresh_auth_required", "label": "Auth required", "tone": "warning", "blockers": ["fresh_story_auth_required"]}
        elif any(b.startswith("story_precheck_not_allowed") for b in hard):
            auth = {**auth, "state": "failed", "label": "Auth failed", "tone": "danger", "blockers": ["story_precheck_not_allowed"]}
    return {
        "state": auth["state"],
        "label": auth["label"],
        "tone": auth["tone"],
        "fresh": bool(auth.get("fresh")),
        "checked_at": auth.get("checked_at") or runtime_row.get("story_precheck_checked_at"),
        "blocked_until": auth.get("blocked_until"),
        "blockers": list(auth.get("blockers") or []),
    }


def _primary_label(
    *,
    governance_allowed: bool,
    runtime_story_ready: bool,
    auth_state: str,
    protected: bool,
    manual_only: bool,
) -> tuple[str, str]:
    if protected:
        return "Protected", "danger"
    if manual_only:
        return "Manual only", "warning"
    if runtime_story_ready:
        return "Story-ready", "success"
    # Failed persisted auth is distinct from "needs a check".
    if auth_state == "failed":
        return "Authentication failed", "danger"
    if auth_state == "blocked":
        return "Blocked", "danger"
    if governance_allowed and auth_state in ("fresh_auth_required", "fresh_auth_stale", "unknown"):
        return "Needs authentication check", "warning"
    if governance_allowed:
        return "Blocked", "danger"
    return "Not for stories", "danger"


def resolve_account_story_readiness(
    db: Session,
    account: Account,
    *,
    purpose_filter: str | None = None,
) -> dict[str, Any]:
    """Normalized layered story readiness for Accounts + Stories UI."""
    aid = int(account.id)
    try:
        gov = resolve_account_governance(db, account)
    except Exception as exc:
        logger.warning("story_readiness_governance_failed", account_id=aid, error=str(exc))
        gov = {
            "protected": aid in PROTECTED_IDS,
            "manual_only": aid in PURPOSE_HOLD_IDS,
            "quarantined": False,
            "ai_reserved": False,
            "roles": [],
            "blocked_reasons": ["governance_unavailable"],
            "badges": [],
        }
    try:
        runtime_row = evaluate_account_story_runtime(db, account, purpose_filter=purpose_filter)
    except Exception as exc:
        logger.warning("story_readiness_runtime_failed", account_id=aid, error=str(exc))
        runtime_row = {
            "account_id": aid,
            "blockers": ["runtime_evaluation_unavailable"],
            "live_only_blockers": [],
            "story_ready": False,
            "dry_run_ready": False,
            "live_ready": False,
            "fresh_story_auth_ok": False,
            "technical_health_ok": False,
            "purpose": (getattr(account, "purpose", None) or "both"),
        }

    governance_allowed = not (
        gov.get("protected") or gov.get("manual_only") or gov.get("quarantined") or gov.get("ai_reserved")
    )
    purpose = (getattr(account, "purpose", None) or "both").strip().lower()
    story_capable = story_purpose_compatible(purpose)
    capability_blockers: list[str] = []
    if not story_capable:
        capability_blockers.append(f"purpose_not_story_compatible:{purpose}")

    db.refresh(account)
    auth = _auth_layer(account, runtime_row)
    dry_run_ready = bool(runtime_row.get("dry_run_ready"))
    live_ready = bool(runtime_row.get("live_ready"))
    # P10.22 operator truth: final runtime-ready requires fresh story auth, not governance-only.
    runtime_story_ready = bool(
        runtime_row.get("story_ready")
        and auth.get("fresh")
        and auth["state"] == "ok"
    )

    primary, severity = _primary_label(
        governance_allowed=governance_allowed,
        runtime_story_ready=runtime_story_ready,
        auth_state=auth["state"],
        protected=bool(gov.get("protected")),
        manual_only=bool(gov.get("manual_only")),
    )

    runtime_blockers = list(runtime_row.get("blockers") or []) + list(runtime_row.get("live_only_blockers") or [])
    if not auth.get("fresh") and governance_allowed and story_capable:
        for blocker in auth.get("blockers") or []:
            if blocker not in runtime_blockers:
                runtime_blockers.append(blocker)

    return {
        "account_id": aid,
        "display": _display_for_account(account),
        "governance": {
            "allowed": governance_allowed,
            "roles": gov.get("roles") or [],
            "blocked_reasons": gov.get("blocked_reasons") or [],
            "protected": bool(gov.get("protected")),
            "manual_only": bool(gov.get("manual_only")),
            "quarantined": bool(gov.get("quarantined")),
            "ai_reserved": bool(gov.get("ai_reserved")),
        },
        "operational": {
            "ok": bool(runtime_row.get("technical_health_ok")),
            "status": runtime_row.get("status"),
            "technical_health": runtime_row.get("health_status"),
            "readiness_status": runtime_row.get("readiness_status"),
        },
        "story_capability": {
            "capable": story_capable,
            "purpose": purpose,
            "blockers": capability_blockers,
        },
        "story_auth": auth,
        "runtime": {
            "dry_run_ready": dry_run_ready,
            "live_ready": live_ready,
            "final_story_ready": runtime_story_ready,
            "blockers": runtime_blockers,
            "dry_run_capable": dry_run_ready,
        },
        "labels": {
            "governance": "Allowed" if governance_allowed else "Blocked",
            "governance_tone": "success" if governance_allowed else "danger",
            "story_auth": auth["label"],
            "story_auth_tone": auth["tone"],
            "runtime": "Story-ready" if runtime_story_ready else "Not ready",
            "runtime_tone": "success" if runtime_story_ready else "warning",
            "primary": primary,
            "severity": severity,
        },
        "governance_story_eligible": governance_allowed and story_capable,
        "governance_allowed": governance_allowed,
        "runtime_story_ready": runtime_story_ready,
        "needs_auth_probe": bool(
            governance_allowed and story_capable and not auth.get("fresh")
        ),
    }


def empty_readiness_preview() -> dict[str, Any]:
    return {
        "ok": True,
        "visibility_only": True,
        "runtime_execution_unchanged": True,
        "summary": {
            "governance_story_eligible": 0,
            "runtime_story_ready": 0,
            "needs_auth_probe": 0,
            "protected": 0,
            "manual_only": 0,
            "quarantined": 0,
            "ai_reserved": 0,
            "blocked": 0,
        },
        "accounts": [],
        "preview_unavailable": True,
    }


def build_story_readiness_preview(
    db: Session,
    *,
    account_ids: list[int] | None = None,
    include_protected: bool = False,
    purpose_filter: str | None = None,
) -> dict[str, Any]:
    """Fleet-level story readiness summary (read-only)."""
    summary = {
        "governance_story_eligible": 0,
        "runtime_story_ready": 0,
        "needs_auth_probe": 0,
        "protected": 0,
        "manual_only": 0,
        "quarantined": 0,
        "ai_reserved": 0,
        "blocked": 0,
    }
    accounts_out: list[dict[str, Any]] = []

    q = db.query(Account).order_by(Account.id.asc())
    if account_ids:
        q = q.filter(Account.id.in_([int(x) for x in account_ids]))
    for account in q.all():
        if not include_protected and int(account.id) in PROTECTED_IDS:
            continue
        try:
            db.refresh(account)
            row = resolve_account_story_readiness(db, account, purpose_filter=purpose_filter)
        except Exception as exc:
            logger.warning("story_readiness_preview_row_failed", account_id=account.id, error=str(exc))
            continue
        gov = row["governance"]
        if gov.get("protected"):
            summary["protected"] += 1
        elif gov.get("manual_only"):
            summary["manual_only"] += 1
        elif gov.get("quarantined"):
            summary["quarantined"] += 1
        elif gov.get("ai_reserved"):
            summary["ai_reserved"] += 1

        if row.get("governance_story_eligible"):
            summary["governance_story_eligible"] += 1
        if row.get("runtime_story_ready"):
            summary["runtime_story_ready"] += 1
        if row.get("needs_auth_probe"):
            summary["needs_auth_probe"] += 1
        if not row.get("runtime_story_ready") and not gov.get("protected") and not gov.get("manual_only"):
            if not row.get("governance_story_eligible"):
                summary["blocked"] += 1

        accounts_out.append(
            {
                "account_id": row["account_id"],
                "display": row["display"],
                "governance_allowed": row.get("governance_allowed"),
                "story_auth_state": row["story_auth"]["state"],
                "story_auth_label": row["story_auth"]["label"],
                "runtime_story_ready": row.get("runtime_story_ready"),
                "governance_story_eligible": row.get("governance_story_eligible"),
                "needs_auth_probe": row.get("needs_auth_probe"),
                "blockers": row["runtime"]["blockers"],
                "labels": row["labels"],
            }
        )

    return {
        "ok": True,
        "visibility_only": True,
        "runtime_execution_unchanged": True,
        "summary": summary,
        "accounts": accounts_out,
    }
