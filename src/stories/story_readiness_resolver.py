"""P10.22 unified story readiness truth (read-only; no execution widening).

Layers (kept separate):
- governance eligibility
- canonical Telegram authorization (fleet readiness matrix when fresh)
- Story capability
- certification history (matrix / durable evidence labels)
- runtime execution enablement (flags + DB precheck; never widened here)
"""
from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy.orm import Session

from src.core.account_protection import PROTECTED_IDS, PURPOSE_HOLD_IDS
from src.core.models import Account
from src.governance.governance_resolver import resolve_account_governance
from src.stories.fleet_readiness_matrix import (
    account_rows_by_id,
    load_latest_matrix,
    matrix_freshness,
)
from src.stories.story_auth_state import resolve_story_auth_state
from src.stories.rotation_audit import evaluate_account_story_runtime, story_purpose_compatible

logger = structlog.get_logger(__name__)

_READY_CLASSES = frozenset(
    {"CERTIFIED_PUBLISH", "READY_FOR_SEPARATE_CONTROLLED_CANARY"}
)


def _display_for_account(account: Account) -> str:
    phone = (getattr(account, "phone_number", None) or "").strip()
    if phone:
        return phone
    username = (getattr(account, "username", None) or "").strip()
    if username:
        return username if username.startswith("@") else f"@{username}"
    return f"#{int(account.id)}"


def _execution_flags_locked() -> bool:
    """True when Story execution pathways are not enabled (expected safe default)."""
    import os

    truthy = frozenset({"1", "true", "yes", "on", "enabled"})
    for name in (
        "STORY_EXECUTION_ENABLED",
        "CONTROLLED_STORY_EXECUTION_ENABLED",
        "SCHEDULER_STORY_EXECUTION_ENABLED",
        "TELEGRAM_STORIES_LIVE_ENABLED",
    ):
        raw = (os.environ.get(name) or "").strip().lower()
        if raw in truthy:
            return False
    return True


def _auth_from_db(account: Account, runtime_row: dict[str, Any]) -> dict[str, Any]:
    """Hydrate auth from persisted Account precheck fields (execution-oriented TTL)."""
    auth = resolve_story_auth_state(account)
    if not auth.get("fresh") and not auth.get("blockers"):
        live_only = list(runtime_row.get("live_only_blockers") or [])
        hard = list(runtime_row.get("blockers") or [])
        if any("fresh_story_auth_stale" in b for b in live_only):
            auth = {
                **auth,
                "state": "fresh_auth_stale",
                "label": "Auth stale",
                "tone": "warning",
                "blockers": ["fresh_story_auth_stale"],
            }
        elif any("fresh_story_auth_required" in b for b in live_only):
            auth = {
                **auth,
                "state": "fresh_auth_required",
                "label": "Auth required",
                "tone": "warning",
                "blockers": ["fresh_story_auth_required"],
            }
        elif any(b.startswith("story_precheck_not_allowed") for b in hard):
            auth = {
                **auth,
                "state": "failed",
                "label": "Auth failed",
                "tone": "danger",
                "blockers": ["story_precheck_not_allowed"],
            }
    return {
        "state": auth["state"],
        "label": auth["label"],
        "tone": auth["tone"],
        "fresh": bool(auth.get("fresh")),
        "checked_at": auth.get("checked_at") or runtime_row.get("story_precheck_checked_at"),
        "blocked_until": auth.get("blocked_until"),
        "blockers": list(auth.get("blockers") or []),
        "source": "db_story_precheck",
    }


def _auth_from_matrix_row(row: dict[str, Any], freshness: dict[str, Any]) -> dict[str, Any]:
    """Map a canonical fleet matrix row into the auth display layer."""
    classification = str(row.get("classification") or "UNKNOWN")
    checked_at = row.get("last_auth_at") or freshness.get("source_audit_completed_at")
    if classification in _READY_CLASSES and row.get("auth_valid") is True:
        return {
            "state": "ok",
            "label": "Auth OK (canonical probe)",
            "tone": "success",
            "fresh": True,
            "checked_at": checked_at,
            "blocked_until": None,
            "blockers": [],
            "source": "canonical_fleet_matrix",
            "classification": classification,
        }
    if classification == "AUTH_STALE":
        return {
            "state": "fresh_auth_stale",
            "label": "Auth stale (canonical)",
            "tone": "warning",
            "fresh": False,
            "checked_at": checked_at,
            "blocked_until": None,
            "blockers": list(row.get("reason_codes") or ["canonical_auth_stale"]),
            "source": "canonical_fleet_matrix",
            "classification": classification,
        }
    if classification in {"AUTH_FAILED", "SESSION_CONFLICT", "SESSION_CORRUPT"}:
        return {
            "state": "failed",
            "label": f"Auth failed ({classification})",
            "tone": "danger",
            "fresh": False,
            "checked_at": checked_at,
            "blocked_until": None,
            "blockers": list(row.get("reason_codes") or [classification.lower()]),
            "source": "canonical_fleet_matrix",
            "classification": classification,
        }
    if classification == "STORY_CAPABILITY_FAILED":
        return {
            "state": "failed",
            "label": "Story capability failed",
            "tone": "danger",
            "fresh": False,
            "checked_at": checked_at,
            "blocked_until": None,
            "blockers": list(row.get("reason_codes") or ["story_capability_failed"]),
            "source": "canonical_fleet_matrix",
            "classification": classification,
        }
    if classification == "CONFIG_INCOMPLETE":
        return {
            "state": "fresh_auth_required",
            "label": "Config incomplete",
            "tone": "warning",
            "fresh": False,
            "checked_at": checked_at,
            "blocked_until": None,
            "blockers": list(row.get("reason_codes") or ["config_incomplete"]),
            "source": "canonical_fleet_matrix",
            "classification": classification,
        }
    if classification in {"ACCOUNT_DISABLED", "INTENTIONALLY_EXCLUDED"}:
        return {
            "state": "blocked",
            "label": classification.replace("_", " ").title(),
            "tone": "danger",
            "fresh": False,
            "checked_at": checked_at,
            "blocked_until": None,
            "blockers": list(row.get("reason_codes") or [classification.lower()]),
            "source": "canonical_fleet_matrix",
            "classification": classification,
        }
    return {
        "state": "unknown",
        "label": f"Unknown ({classification})",
        "tone": "warning",
        "fresh": False,
        "checked_at": checked_at,
        "blocked_until": None,
        "blockers": list(row.get("reason_codes") or ["canonical_unknown"]),
        "source": "canonical_fleet_matrix",
        "classification": classification,
    }


def _auth_layer(
    account: Account,
    runtime_row: dict[str, Any],
    *,
    matrix_row: dict[str, Any] | None,
    freshness: dict[str, Any],
) -> dict[str, Any]:
    """Prefer fresh canonical matrix auth; fall back to DB precheck TTL."""
    if freshness.get("fresh") and matrix_row is not None and matrix_row.get("probed"):
        return _auth_from_matrix_row(matrix_row, freshness)
    if freshness.get("fresh") and matrix_row is not None and not matrix_row.get("probed"):
        # Excluded / not probed rows still come from the canonical inventory.
        return _auth_from_matrix_row(matrix_row, freshness)
    return _auth_from_db(account, runtime_row)


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
    if governance_allowed and auth_state == "ok":
        return "Authorized (execution locked)", "info"
    if governance_allowed:
        return "Blocked", "danger"
    return "Not for stories", "danger"


def resolve_account_story_readiness(
    db: Session,
    account: Account,
    *,
    purpose_filter: str | None = None,
    matrix_row: dict[str, Any] | None = None,
    freshness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalized layered story readiness for Accounts + Stories UI."""
    aid = int(account.id)
    freshness = freshness or matrix_freshness(None)
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

    # Canonical matrix may refine Story capability from live CanSendStoryRequest.
    canonical_story_capable = story_capable
    if freshness.get("fresh") and matrix_row and matrix_row.get("probed"):
        if matrix_row.get("story_api_available") is True:
            canonical_story_capable = True
        elif matrix_row.get("story_api_available") is False:
            canonical_story_capable = False
            capability_blockers.append(
                f"canonical_story_probe:{matrix_row.get('story_probe_status') or 'failed'}"
            )

    db.refresh(account)
    auth = _auth_layer(account, runtime_row, matrix_row=matrix_row, freshness=freshness)
    db_auth = _auth_from_db(account, runtime_row)
    dry_run_ready = bool(runtime_row.get("dry_run_ready"))
    live_ready = bool(runtime_row.get("live_ready"))

    # Execution-oriented runtime ready: never widened by matrix alone.
    # Requires DB fresh precheck + runtime story_ready; stays false while flags locked.
    execution_locked = _execution_flags_locked()
    runtime_story_ready = bool(
        (not execution_locked)
        and runtime_row.get("story_ready")
        and db_auth.get("fresh")
        and db_auth["state"] == "ok"
    )

    canonical_authorized = bool(
        freshness.get("fresh")
        and matrix_row
        and matrix_row.get("auth_valid") is True
        and matrix_row.get("identity_matches") is True
        and str(matrix_row.get("classification") or "") in _READY_CLASSES
    )
    certified_history = bool(
        matrix_row and matrix_row.get("controlled_publish_certified")
    ) or str((matrix_row or {}).get("classification") or "") == "CERTIFIED_PUBLISH"

    primary, severity = _primary_label(
        governance_allowed=governance_allowed,
        runtime_story_ready=runtime_story_ready,
        auth_state=auth["state"],
        protected=bool(gov.get("protected")),
        manual_only=bool(gov.get("manual_only")),
    )

    runtime_blockers = list(runtime_row.get("blockers") or []) + list(
        runtime_row.get("live_only_blockers") or []
    )
    if execution_locked:
        runtime_blockers.append("execution_flags_locked")
    if not auth.get("fresh") and governance_allowed and story_capable:
        for blocker in auth.get("blockers") or []:
            if blocker not in runtime_blockers:
                runtime_blockers.append(blocker)

    needs_auth_probe = bool(
        governance_allowed
        and story_capable
        and not canonical_authorized
        and auth.get("state") in ("fresh_auth_required", "fresh_auth_stale", "unknown", "failed")
    )
    # Config-incomplete publishing accounts still need operator attention.
    if (
        governance_allowed
        and story_capable
        and matrix_row
        and str(matrix_row.get("classification") or "") == "CONFIG_INCOMPLETE"
    ):
        needs_auth_probe = True

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
            "capable": canonical_story_capable,
            "purpose": purpose,
            "blockers": capability_blockers,
        },
        "story_auth": auth,
        "certification_history": {
            "controlled_publish_certified": certified_history,
            "classification": (matrix_row or {}).get("classification"),
        },
        "runtime": {
            "dry_run_ready": dry_run_ready,
            "live_ready": live_ready,
            "final_story_ready": runtime_story_ready,
            "execution_flags_locked": execution_locked,
            "blockers": runtime_blockers,
            "dry_run_capable": dry_run_ready,
        },
        "canonical_matrix": {
            "fresh": bool(freshness.get("fresh")),
            "label": freshness.get("label"),
            "generated_at": freshness.get("generated_at"),
            "source_audit_run_id": freshness.get("source_audit_run_id"),
            "classification": (matrix_row or {}).get("classification"),
            "probed": bool((matrix_row or {}).get("probed")),
        },
        "labels": {
            "governance": "Allowed" if governance_allowed else "Blocked",
            "governance_tone": "success" if governance_allowed else "danger",
            "story_auth": auth["label"],
            "story_auth_tone": auth["tone"],
            "runtime": "Story-ready" if runtime_story_ready else (
                "Execution locked" if execution_locked else "Not ready"
            ),
            "runtime_tone": "success" if runtime_story_ready else (
                "info" if execution_locked and canonical_authorized else "warning"
            ),
            "primary": primary,
            "severity": severity,
        },
        "governance_story_eligible": governance_allowed and story_capable,
        "governance_allowed": governance_allowed,
        "canonical_authorized": canonical_authorized,
        "runtime_story_ready": runtime_story_ready,
        "needs_auth_probe": needs_auth_probe,
    }


def empty_readiness_preview() -> dict[str, Any]:
    return {
        "ok": True,
        "visibility_only": True,
        "runtime_execution_unchanged": True,
        "summary": {
            "governance_story_eligible": 0,
            "canonical_authorized": 0,
            "runtime_story_ready": 0,
            "needs_auth_probe": 0,
            "protected": 0,
            "manual_only": 0,
            "quarantined": 0,
            "ai_reserved": 0,
            "blocked": 0,
        },
        "accounts": [],
        "canonical_freshness": matrix_freshness(None),
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
    matrix = load_latest_matrix()
    freshness = matrix_freshness(matrix)
    by_id = account_rows_by_id(matrix)
    summary = {
        "governance_story_eligible": 0,
        "canonical_authorized": 0,
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
            row = resolve_account_story_readiness(
                db,
                account,
                purpose_filter=purpose_filter,
                matrix_row=by_id.get(int(account.id)),
                freshness=freshness,
            )
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
        if row.get("canonical_authorized"):
            summary["canonical_authorized"] += 1
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
                "canonical_authorized": row.get("canonical_authorized"),
                "needs_auth_probe": row.get("needs_auth_probe"),
                "blockers": row["runtime"]["blockers"],
                "labels": row["labels"],
                "canonical_classification": (row.get("canonical_matrix") or {}).get("classification"),
            }
        )

    return {
        "ok": True,
        "visibility_only": True,
        "runtime_execution_unchanged": True,
        "summary": summary,
        "accounts": accounts_out,
        "canonical_freshness": freshness,
        "canonical_source": freshness.get("canonical_source"),
    }
