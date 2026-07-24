"""Canonical fleet readiness matrix: inventory roles + audit classifications.

Read-only assembly. Does not publish Stories, send messages, or mutate sessions.
Persists operator-facing JSON under the shared data directory only.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.ai_agent.account_allowlist import RESERVED_AI_AGENT_ACCOUNT_IDS
from src.core.account_operational_state import CONTROLLER_ACCOUNT_IDS
from src.core.account_protection import PROTECTED_IDS, PURPOSE_HOLD_IDS
from src.core.database import get_db_context
from src.core.models import Account
from src.stories.daily_story_counter import production_story_day, stories_today_effective
from src.stories.fleet_certification import (
    calculate_totals,
    durable_certification_evidence,
    inspect_session,
    mask_phone,
    normalize_classification,
)

PUBLISHING_ROLES = frozenset({"PUBLISHING_ACCOUNT", "CERTIFIED_REFERENCE"})
EXCLUDED_ROLES = frozenset(
    {
        "PROTECTED_CONTROLLER",
        "AI_OR_INFRASTRUCTURE_RESERVED",
        "INTENTIONALLY_DISABLED",
        "NOT_CONFIGURED_FOR_STORIES",
    }
)

DEFAULT_LATEST_PATH = Path("/opt/autostory/data/fleet-readiness/latest.json")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def classify_operational_role(account: Account, certified_ids: set[int]) -> str:
    aid = int(account.id)
    status = str(getattr(account.status, "value", account.status) or "").lower()
    purpose = (account.purpose or "both").strip().lower()
    enabled = status == "active" and purpose != "disabled"
    if aid in CONTROLLER_ACCOUNT_IDS or aid in PROTECTED_IDS:
        return "PROTECTED_CONTROLLER"
    if aid in RESERVED_AI_AGENT_ACCOUNT_IDS:
        return "AI_OR_INFRASTRUCTURE_RESERVED"
    if aid in PURPOSE_HOLD_IDS or purpose == "disabled" or not enabled:
        return "INTENTIONALLY_DISABLED"
    if purpose in {"", "both", "story", "stories", "autostory"}:
        if aid in certified_ids:
            return "CERTIFIED_REFERENCE"
        return "PUBLISHING_ACCOUNT"
    if purpose in {"discovery", "campaign", "broadcast"}:
        return "NOT_CONFIGURED_FOR_STORIES"
    return "UNKNOWN_ROLE"


def operator_action_for(classification: str, role: str) -> str | None:
    if classification == "INTENTIONALLY_EXCLUDED":
        return f"None — preserve {role} role; do not force Story readiness"
    if classification == "ACCOUNT_DISABLED":
        return "None — leave disabled unless product explicitly re-enables"
    if classification == "AUTH_FAILED":
        return (
            "Sequential operator reauth via dashboard /accounts auth flow: "
            "OTP then 2FA if required; verify identity + CanSendStory; do not mark ready on login alone"
        )
    if classification == "AUTH_STALE":
        return "Re-run isolated read-only fleet probe for this account; if unauthorized, follow AUTH_FAILED queue"
    if classification == "SESSION_CORRUPT":
        return "Prove session unusable, backup metadata, recreate via canonical session path for this account only"
    if classification == "IDENTITY_MISMATCH":
        return "Operator review: reconcile configured Telegram identity with live get_me before any publish"
    if classification == "STORY_CAPABILITY_FAILED":
        return "Operator review: Telegram Story capability denied for non-limit reason"
    if classification == "SESSION_CONFLICT":
        return "Stop conflicting worker lock on this account; confirm disposable-session readiness path"
    if classification == "COUNTER_INVALID":
        return "Reconcile stories_today via application path from canonical Story history"
    if classification == "CONFIG_INCOMPLETE":
        return "Complete canonical account mapping (phone, identity, session reference)"
    if classification == "UNKNOWN":
        return "CONFIG_REVIEW_REQUIRED — resolve operational role before probing"
    return None


def build_excluded_row(account: Account, role: str, certification: dict[str, Any] | None) -> dict[str, Any]:
    status = str(getattr(account.status, "value", account.status) or "").lower()
    insp = inspect_session(account)
    final = (
        "ACCOUNT_DISABLED"
        if role == "INTENTIONALLY_DISABLED"
        else "INTENTIONALLY_EXCLUDED"
    )
    display = " ".join(
        part for part in [account.first_name, account.last_name] if part
    ).strip() or (f"@{account.username}" if account.username else f"Account {account.id}")
    return {
        "account_id": int(account.id),
        "display_name": display,
        "intended_operational_role": role,
        "configured_enabled": status == "active" and (account.purpose or "").lower() != "disabled",
        "configured_status": status or "UNKNOWN",
        "purpose": account.purpose or "UNKNOWN",
        "expected_telegram_user_id": int(account.user_id) if account.user_id is not None else None,
        "expected_username": account.username,
        "phone_redacted": mask_phone(account.phone_number),
        "session_type": insp.kind,
        "session_present": insp.present,
        "session_readable": insp.readable,
        "auth_valid": None,
        "telegram_user_id": None,
        "telegram_username": None,
        "identity_matches": None,
        "story_api_available": None,
        "story_probe_status": "not_probed_excluded",
        "controlled_publish_certified": bool(certification),
        "stories_today_effective": stories_today_effective(account),
        "daily_limit": 1,
        "production_day": str(production_story_day()),
        "classification": final,
        "reason_codes": [f"role_{role.lower()}"],
        "required_operator_action": operator_action_for(final, role),
        "probed": False,
    }


def merge_audit_row(
    account: Account,
    role: str,
    audit_row: dict[str, Any],
) -> dict[str, Any]:
    classification = normalize_classification(str(audit_row.get("classification") or "UNKNOWN"))
    row = {
        "account_id": int(account.id),
        "display_name": audit_row.get("display_name"),
        "intended_operational_role": role,
        "configured_enabled": audit_row.get("configured_enabled"),
        "configured_status": audit_row.get("configured_status"),
        "purpose": audit_row.get("purpose"),
        "expected_telegram_user_id": audit_row.get("expected_telegram_user_id"),
        "expected_username": audit_row.get("expected_username"),
        "phone_redacted": audit_row.get("phone_redacted"),
        "session_type": audit_row.get("session_type"),
        "session_present": audit_row.get("session_present"),
        "session_readable": audit_row.get("session_readable"),
        "auth_valid": audit_row.get("auth_valid"),
        "telegram_user_id": audit_row.get("telegram_user_id"),
        "telegram_username": audit_row.get("telegram_username"),
        "identity_matches": audit_row.get("identity_matches"),
        "story_api_available": audit_row.get("story_api_available"),
        "story_probe_status": audit_row.get("story_probe_status"),
        "controlled_publish_certified": audit_row.get("controlled_publish_certified"),
        "last_auth_at": audit_row.get("last_auth_at"),
        "last_story_at": audit_row.get("last_story_at"),
        "stories_today_effective": stories_today_effective(account),
        "daily_limit": 1,
        "production_day": str(production_story_day()),
        "classification": classification,
        "reason_codes": list(audit_row.get("reason_codes") or []),
        "safe_error_summary": audit_row.get("safe_error_summary"),
        "required_operator_action": operator_action_for(classification, role),
        "probed": True,
        "audit_run_id": audit_row.get("audit_run_id"),
    }
    return row


def build_canonical_matrix(audit_report: dict[str, Any]) -> dict[str, Any]:
    """Combine full DB inventory with an in-scope audit report (one row per account)."""
    audit_by_id = {
        int(row["account_id"]): row for row in (audit_report.get("accounts") or [])
    }
    with get_db_context() as db:
        accounts = db.query(Account).order_by(Account.id.asc()).all()
        certs = durable_certification_evidence(db)
        certified_ids = set(certs.keys())

    rows: list[dict[str, Any]] = []
    for account in accounts:
        role = classify_operational_role(account, certified_ids)
        aid = int(account.id)
        if role in EXCLUDED_ROLES or role == "UNKNOWN_ROLE":
            rows.append(build_excluded_row(account, role, certs.get(aid)))
            continue
        audit_row = audit_by_id.get(aid)
        if audit_row is None:
            rows.append(
                {
                    "account_id": aid,
                    "display_name": f"Account {aid}",
                    "intended_operational_role": role,
                    "classification": "UNKNOWN",
                    "reason_codes": ["missing_fresh_audit_row"],
                    "required_operator_action": operator_action_for("UNKNOWN", role),
                    "probed": False,
                }
            )
            continue
        rows.append(merge_audit_row(account, role, audit_row))

    rows.sort(key=lambda r: int(r["account_id"]))
    class_hist = Counter(r["classification"] for r in rows)
    role_hist = Counter(r.get("intended_operational_role") or "UNKNOWN_ROLE" for r in rows)
    probe_totals = calculate_totals(
        [
            r
            for r in rows
            if r.get("intended_operational_role") in PUBLISHING_ROLES
        ]
    )
    return {
        "schema": "storyfleet_fleet_readiness_matrix_v1",
        "generated_at": utc_now_iso(),
        "source_audit_run_id": audit_report.get("audit_run_id"),
        "source_audit_completed_at": audit_report.get("audit_completed_at"),
        "production_day": str(production_story_day()),
        "totals": {
            "total_configured_accounts": len(rows),
            "publishing_accounts": role_hist.get("PUBLISHING_ACCOUNT", 0)
            + role_hist.get("CERTIFIED_REFERENCE", 0),
            "certified_accounts": class_hist.get("CERTIFIED_PUBLISH", 0),
            "ready_for_separate_controlled_canary": class_hist.get(
                "READY_FOR_SEPARATE_CONTROLLED_CANARY", 0
            ),
            "intentionally_excluded": class_hist.get("INTENTIONALLY_EXCLUDED", 0),
            "disabled": class_hist.get("ACCOUNT_DISABLED", 0),
            "need_operator_authentication": class_hist.get("AUTH_FAILED", 0)
            + class_hist.get("AUTH_STALE", 0),
            "broken_sessions": class_hist.get("SESSION_CORRUPT", 0),
            "configuration_failures": class_hist.get("CONFIG_INCOMPLETE", 0),
            "identity_mismatches": class_hist.get("IDENTITY_MISMATCH", 0),
            "story_capability_failures": class_hist.get("STORY_CAPABILITY_FAILED", 0),
            "session_conflicts": class_hist.get("SESSION_CONFLICT", 0),
            "counter_failures": class_hist.get("COUNTER_INVALID", 0),
            "unknown": class_hist.get("UNKNOWN", 0),
            "classification_histogram": dict(class_hist),
            "role_histogram": dict(role_hist),
            "in_scope_probe_totals": probe_totals,
        },
        "accounts": rows,
        "publish_controls": False,
        "secrets_redacted": True,
    }


def write_latest_matrix(matrix: dict[str, Any], path: Path = DEFAULT_LATEST_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(matrix, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    return path


def load_latest_matrix(path: Path = DEFAULT_LATEST_PATH) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
