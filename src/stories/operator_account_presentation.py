"""Operator-facing presentation mapper for canonical fleet readiness.

Single deterministic mapper shared by Accounts, Fleet Readiness, diagnostics,
filters, badges, and summary totals. Presentation only — no Telegram I/O,
session mutation, or execution-flag changes.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

OPERATOR_STATUSES = (
    "READY",
    "CERTIFIED",
    "NEEDS_SESSION",
    "DISABLED",
    "PROTECTED",
    "RESERVED",
    "BLOCKED",
    "CHECK_REQUIRED",
)

# Owner-facing health vocabulary (PROTECTED/RESERVED present as Unavailable).
OWNER_HEALTH_LABELS = {
    "READY": "Ready",
    "CERTIFIED": "Certified",
    "NEEDS_SESSION": "Needs session",
    "BLOCKED": "Blocked",
    "CHECK_REQUIRED": "Check required",
    "DISABLED": "Disabled",
    "PROTECTED": "Unavailable",
    "RESERVED": "Unavailable",
}

_DEFAULT_TTL_HOURS = 24

_BLOCKED_CLASSIFICATIONS = frozenset(
    {
        "AUTH_FAILED",
        "IDENTITY_MISMATCH",
        "STORY_CAPABILITY_FAILED",
        "SESSION_CONFLICT",
        "SESSION_CORRUPT",
        "FLOOD_WAIT",
        "BANNED",
        "COUNTER_INVALID",
        "UNKNOWN",
    }
)

_SESSION_REASON_MARKERS = frozenset(
    {
        "session_missing",
        "missing_account_config",
        "session_unreadable",
        "session_corrupt",
    }
)

_SORT_PRIORITY = {
    "BLOCKED": 0,
    "NEEDS_SESSION": 1,
    "CHECK_REQUIRED": 2,
    "READY": 10,
    "CERTIFIED": 11,
    "DISABLED": 20,
    "PROTECTED": 30,
    "RESERVED": 31,
}


def _matrix_helpers():
    """Lazy import keeps this mapper free of Telegram/session certification deps."""
    from src.stories.fleet_readiness_matrix import (
        FLEET_MATRIX_FRESH_TTL_HOURS,
        account_rows_by_id,
        load_latest_matrix,
        matrix_freshness,
    )

    return load_latest_matrix, matrix_freshness, account_rows_by_id, FLEET_MATRIX_FRESH_TTL_HOURS


def format_operator_timestamp(value: Any) -> str | None:
    """UTC display timestamp for operator tables (no secrets)."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return f"{dt.strftime('%B')} {dt.day}, {dt.strftime('%Y %H:%M')} UTC"


# Back-compat alias used by older call sites / tests.
_iso_display = format_operator_timestamp


def _role_label(role: str | None) -> str:
    mapping = {
        "PUBLISHING_ACCOUNT": "Publishing",
        "CERTIFIED_REFERENCE": "Publishing",
        "INTENTIONALLY_DISABLED": "Disabled",
        "PROTECTED_CONTROLLER": "Protected",
        "AI_OR_INFRASTRUCTURE_RESERVED": "AI account",
        "NOT_CONFIGURED_FOR_STORIES": "Not configured",
        "UNKNOWN_ROLE": "Unknown",
    }
    return mapping.get(str(role or "UNKNOWN_ROLE"), str(role or "Unknown"))


def _blocked_reason(row: dict[str, Any]) -> str:
    classification = str(row.get("classification") or "UNKNOWN")
    reasons = [str(r) for r in (row.get("reason_codes") or [])]
    summary = (row.get("safe_error_summary") or "").strip()
    if classification == "AUTH_FAILED":
        if summary and "not authorized" in summary.lower():
            return "Authentication failed"
        return summary or "Authentication failed"
    if classification == "IDENTITY_MISMATCH":
        return "Identity mismatch"
    if classification == "STORY_CAPABILITY_FAILED":
        return "Story publishing not allowed"
    if classification == "SESSION_CONFLICT":
        return "Session conflict"
    if classification == "SESSION_CORRUPT":
        return "Session unusable"
    if classification == "FLOOD_WAIT":
        return "Temporarily rate limited"
    if classification == "BANNED":
        return "Account banned or deactivated"
    if classification == "COUNTER_INVALID":
        return "Session counter invalid"
    if summary:
        return summary[:120]
    if reasons:
        return reasons[0].replace("_", " ")
    return classification.replace("_", " ").title()


def sort_priority_for_status(display_status: str) -> int:
    return int(_SORT_PRIORITY.get(str(display_status or ""), 50))


def map_canonical_account(
    row: dict[str, Any] | None,
    *,
    freshness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Map one canonical matrix row to operator presentation fields."""
    freshness = freshness or {"fresh": False, "label": "MISSING", "present": False}
    row = row or {}
    aid = int(row.get("account_id") or 0)
    classification = str(row.get("classification") or "UNKNOWN")
    role = str(row.get("intended_operational_role") or "UNKNOWN_ROLE")
    reasons = [str(r) for r in (row.get("reason_codes") or [])]
    auth_valid = row.get("auth_valid")
    story_ok = bool(row.get("story_api_available") is True) or (
        auth_valid is True and classification in {"CERTIFIED_PUBLISH", "READY_FOR_SEPARATE_CONTROLLED_CANARY"}
    )
    matrix_fresh = bool(freshness.get("fresh"))

    # Precedence: structural unavailability before auth/capability states.
    if (
        classification == "ACCOUNT_DISABLED"
        or role == "INTENTIONALLY_DISABLED"
        or str(row.get("purpose") or "").lower() == "disabled"
    ):
        display_status = "DISABLED"
        status_label = "Disabled"
        status_detail = "Intentionally disabled"
        authorization_label = "Unavailable"
        required_action = "None"
        severity = "info"
        diagnostic_reason = "purpose_disabled" if str(row.get("purpose") or "").lower() == "disabled" else "account_disabled"
        row_action = "view_details"
    elif role == "PROTECTED_CONTROLLER" or (
        classification == "INTENTIONALLY_EXCLUDED" and role == "PROTECTED_CONTROLLER"
    ):
        display_status = "PROTECTED"
        status_label = "Unavailable"
        status_detail = "Protected — not for Stories"
        authorization_label = "Unavailable"
        required_action = "None"
        severity = "danger"
        diagnostic_reason = "protected_controller"
        row_action = "view_details"
    elif role == "AI_OR_INFRASTRUCTURE_RESERVED" or (
        classification == "INTENTIONALLY_EXCLUDED" and role == "AI_OR_INFRASTRUCTURE_RESERVED"
    ):
        display_status = "RESERVED"
        status_label = "Unavailable"
        status_detail = "Reserved — not for Stories"
        authorization_label = "Unavailable"
        required_action = "None"
        severity = "reserved"
        diagnostic_reason = "ai_or_infrastructure_reserved"
        row_action = "view_details"
    elif classification == "INTENTIONALLY_EXCLUDED":
        display_status = "PROTECTED"
        status_label = "Unavailable"
        status_detail = "Excluded — not for Stories"
        authorization_label = "Unavailable"
        required_action = "None"
        severity = "danger"
        diagnostic_reason = "intentionally_excluded"
        row_action = "view_details"
    elif classification == "CONFIG_INCOMPLETE" or any(r in _SESSION_REASON_MARKERS for r in reasons) or (
        row.get("session_present") is False and role in {"PUBLISHING_ACCOUNT", "CERTIFIED_REFERENCE"}
    ):
        display_status = "NEEDS_SESSION"
        status_label = "Needs session"
        status_detail = "Session setup required"
        authorization_label = "Needs login/session"
        required_action = "Import or restore session"
        severity = "warning"
        diagnostic_reason = "session_missing" if "session_missing" in reasons or row.get("session_present") is False else "config_incomplete"
        row_action = "import_session"
    elif classification in _BLOCKED_CLASSIFICATIONS:
        display_status = "BLOCKED"
        reason = _blocked_reason(row)
        status_label = "Blocked"
        status_detail = reason
        authorization_label = (
            "Needs login/session" if classification in {"AUTH_FAILED", "SESSION_CORRUPT", "SESSION_CONFLICT"} else "Not ready"
        )
        required_action = reason
        severity = "danger"
        diagnostic_reason = classification.lower()
        row_action = "view_details"
    elif not matrix_fresh and role in {"PUBLISHING_ACCOUNT", "CERTIFIED_REFERENCE"}:
        # Never present stale READY/CERTIFIED as current truth.
        display_status = "CHECK_REQUIRED"
        status_label = "Check required"
        status_detail = "Health check required"
        authorization_label = "Needs check"
        required_action = "Refresh fleet authorization"
        severity = "warning"
        diagnostic_reason = "canonical_matrix_stale"
        row_action = "view_details"
    elif (
        classification == "CERTIFIED_PUBLISH"
        and auth_valid is True
        and story_ok
        and matrix_fresh
    ):
        display_status = "CERTIFIED"
        status_label = "Certified"
        status_detail = "Ready to use"
        authorization_label = "Authorized"
        required_action = "None"
        severity = "success"
        diagnostic_reason = "durable_controlled_story_evidence"
        row_action = "view_details"
    elif (
        classification == "READY_FOR_SEPARATE_CONTROLLED_CANARY"
        and auth_valid is True
        and story_ok
        and matrix_fresh
    ):
        display_status = "READY"
        status_label = "Ready"
        status_detail = "Can run controlled Story"
        authorization_label = "Authorized"
        required_action = "Controlled canary"
        severity = "success"
        diagnostic_reason = "ready_for_separate_controlled_canary"
        row_action = "view_details"
    else:
        display_status = "CHECK_REQUIRED"
        status_label = "Check required"
        status_detail = "Health check required"
        authorization_label = "Needs check"
        required_action = "Refresh fleet authorization"
        severity = "warning"
        diagnostic_reason = "unmapped_or_incomplete_probe"
        row_action = "view_details"

    phone = row.get("phone_redacted") or ""
    username = row.get("expected_username") or row.get("telegram_username")
    if username and not str(username).startswith("@"):
        username_disp = f"@{username}"
    else:
        username_disp = username
    account_label = phone or username_disp or row.get("display_name") or f"account_{aid}"

    tooltip = None
    if display_status == "CERTIFIED":
        tooltip = (
            "Previously completed controlled Story certification and passed the "
            "latest canonical authorization probe."
        )
    elif display_status == "CHECK_REQUIRED":
        tooltip = "Certification evidence is missing or expired — not necessarily a broken account."

    filter_group = {
        "READY": "ready",
        "CERTIFIED": "ready",
        "NEEDS_SESSION": "needs_attention",
        "BLOCKED": "needs_attention",
        "CHECK_REQUIRED": "needs_attention",
        "DISABLED": "unavailable",
        "PROTECTED": "unavailable",
        "RESERVED": "unavailable",
    }.get(display_status, "all")

    return {
        "account_id": aid,
        "account_label": account_label,
        "display_name": row.get("display_name"),
        "phone_redacted": phone,
        "username": username_disp,
        "telegram_user_id": row.get("telegram_user_id") or row.get("expected_telegram_user_id"),
        "display_status": display_status,
        "status_label": status_label,
        "status_detail": status_detail,
        "authorization_label": authorization_label,
        "role_label": _role_label(role),
        "required_action": required_action,
        "severity": severity,
        "diagnostic_reason": diagnostic_reason,
        "row_action": row_action,
        "tooltip": tooltip,
        "sort_priority": sort_priority_for_status(display_status),
        "last_checked": row.get("last_auth_at") or freshness.get("source_audit_completed_at") or freshness.get("generated_at"),
        "last_story_at_matrix": row.get("last_story_at"),
        "filter_group": filter_group,
        "canonical": {
            "classification": classification,
            "intended_operational_role": role,
            "auth_valid": auth_valid,
            "identity_matches": row.get("identity_matches"),
            "story_api_available": row.get("story_api_available"),
            "story_probe_status": row.get("story_probe_status"),
            "reason_codes": reasons,
            "safe_error_summary": row.get("safe_error_summary"),
            "session_present": row.get("session_present"),
            "session_readable": row.get("session_readable"),
            "session_type": row.get("session_type"),
            "configured_status": row.get("configured_status"),
            "purpose": row.get("purpose"),
            "controlled_publish_certified": row.get("controlled_publish_certified"),
            "probed": row.get("probed"),
            "required_operator_action": row.get("required_operator_action"),
            "last_auth_at": row.get("last_auth_at"),
            "last_story_at": row.get("last_story_at"),
            "flood_wait_seconds": row.get("flood_wait_seconds"),
        },
    }


def summarize_operator_accounts(mapped: list[dict[str, Any]], *, freshness: dict[str, Any]) -> dict[str, Any]:
    counts = Counter(row["display_status"] for row in mapped)
    certified = counts["CERTIFIED"]
    ready = counts["READY"]
    needs_session = counts["NEEDS_SESSION"]
    disabled = counts["DISABLED"]
    protected = counts["PROTECTED"]
    reserved = counts["RESERVED"]
    blocked = counts["BLOCKED"]
    check_required = counts["CHECK_REQUIRED"]
    authorized = certified + ready
    needs_attention = needs_session + blocked + check_required
    unavailable = disabled + protected + reserved
    default_filter = "needs_attention" if needs_attention else "ready"
    generated = freshness.get("generated_at") or freshness.get("source_audit_completed_at")
    return {
        "total_accounts": len(mapped),
        "authorized": authorized,
        "certified": certified,
        "ready": ready,
        "needs_session": needs_session,
        "needs_attention": needs_attention,
        "disabled": disabled,
        "protected": protected,
        "reserved": reserved,
        "unavailable": unavailable,
        "blocked": blocked,
        "check_required": check_required,
        "default_filter": default_filter,
        "automation_locked": True,
        "freshness_compact": {
            "last_verified": _iso_display(generated),
            "valid_for_hours": int(freshness.get("ttl_hours") or _DEFAULT_TTL_HOURS),
            "source_label": (
                "Live fleet probe"
                if freshness.get("label") == "FRESH_LIVE_PROBE"
                else (
                    "Readiness expired — health check required"
                    if not freshness.get("fresh")
                    else "Cached matrix"
                )
            ),
            "fresh": bool(freshness.get("fresh")),
            "label": freshness.get("label"),
            "generated_at": freshness.get("generated_at"),
            "source_audit_run_id": freshness.get("source_audit_run_id"),
            "canonical_source": freshness.get("canonical_source"),
            "ttl_hours": freshness.get("ttl_hours"),
        },
        "headline": {
            "total": len(mapped),
            "authorized": authorized,
            "certified": certified,
            "ready": ready,
            "needs_session": needs_session,
            "disabled": disabled,
            "reserved_protected": protected + reserved,
        },
    }


def build_operator_account_views(
    matrix: dict[str, Any] | None = None,
    *,
    freshness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build mapped rows + summary from the canonical readiness artifact."""
    by_id: dict[int, dict[str, Any]] = {}
    if matrix is None or freshness is None:
        load_latest_matrix, matrix_freshness, account_rows_by_id, _ttl = _matrix_helpers()
        matrix = matrix if matrix is not None else load_latest_matrix()
        freshness = freshness if freshness is not None else matrix_freshness(matrix)
        by_id = account_rows_by_id(matrix)
    else:
        for row in (matrix or {}).get("accounts") or []:
            try:
                by_id[int(row["account_id"])] = row
            except (KeyError, TypeError, ValueError):
                continue
    mapped = [map_canonical_account(row, freshness=freshness) for row in (matrix or {}).get("accounts") or []]
    mapped.sort(key=lambda r: (sort_priority_for_status(r["display_status"]), int(r["account_id"])))
    summary = summarize_operator_accounts(mapped, freshness=freshness)
    return {
        "accounts": mapped,
        "accounts_by_id": {int(r["account_id"]): r for r in mapped},
        "summary": summary,
        "freshness": freshness,
        "matrix_present": bool(matrix),
        "canonical_source": freshness.get("canonical_source"),
        "raw_by_id": by_id,
    }

CONTRADICTORY_PRIMARY_LABELS = frozenset(
    {
        "needs repair",
        "technical health",
        "governance: allowed",
        "execution locked",
        "runtime execution-ready",
        "operational",
        "story runtime",
        "scheduler: eligible",
        "discovery: blocked",
        "campaigns: governance required",
    }
)


def primary_table_text_is_clean(text: str) -> bool:
    """True when main-table markup avoids contradictory primary labels."""
    lowered = (text or "").lower()
    return not any(token in lowered for token in CONTRADICTORY_PRIMARY_LABELS)
