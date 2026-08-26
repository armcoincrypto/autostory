"""Focused tests for operator account presentation mapper."""
from __future__ import annotations

from pathlib import Path

from src.stories.operator_account_presentation import (
    build_operator_account_views,
    map_canonical_account,
    primary_table_text_is_clean,
    summarize_operator_accounts,
)


def _fresh(**overrides):
    base = {
        "present": True,
        "fresh": True,
        "label": "FRESH_LIVE_PROBE",
        "generated_at": "2026-07-27T19:26:37.299331Z",
        "source_audit_completed_at": "2026-07-27T19:26:37.234335Z",
        "ttl_hours": 24,
        "canonical_source": "/opt/autostory/data/fleet-readiness/latest.json",
    }
    base.update(overrides)
    return base


def _row(**overrides):
    base = {
        "account_id": 1,
        "display_name": "Test",
        "phone_redacted": "+18***0001",
        "intended_operational_role": "PUBLISHING_ACCOUNT",
        "classification": "READY_FOR_SEPARATE_CONTROLLED_CANARY",
        "auth_valid": True,
        "identity_matches": True,
        "story_api_available": True,
        "story_probe_status": "allowed",
        "session_present": True,
        "session_readable": True,
        "purpose": "both",
        "configured_status": "active",
        "reason_codes": [],
        "probed": True,
        "last_auth_at": "2026-07-27T19:26:00Z",
    }
    base.update(overrides)
    return base


def test_certified_display_mapping():
    mapped = map_canonical_account(
        _row(
            account_id=106,
            classification="CERTIFIED_PUBLISH",
            intended_operational_role="CERTIFIED_REFERENCE",
            controlled_publish_certified=True,
        ),
        freshness=_fresh(),
    )
    assert mapped["display_status"] == "CERTIFIED"
    assert mapped["status_label"] == "Certified"
    assert mapped["authorization_label"] == "Auth OK"
    assert mapped["required_action"] == "None"
    assert "certification" in (mapped["tooltip"] or "").lower()


def test_ready_display_mapping():
    mapped = map_canonical_account(_row(account_id=108), freshness=_fresh())
    assert mapped["display_status"] == "READY"
    assert mapped["status_label"] == "Ready"
    assert mapped["authorization_label"] == "Auth OK"
    assert mapped["required_action"] == "Controlled canary"
    assert "execution" not in mapped["status_detail"].lower()


def test_missing_session_mapping_account_107():
    mapped = map_canonical_account(
        _row(
            account_id=107,
            classification="CONFIG_INCOMPLETE",
            auth_valid=None,
            story_api_available=None,
            session_present=False,
            reason_codes=["session_missing"],
            phone_redacted="+15***0107",
        ),
        freshness=_fresh(),
    )
    assert mapped["display_status"] == "NEEDS_SESSION"
    assert mapped["status_label"] == "Needs session"
    assert mapped["authorization_label"] == "Not configured"
    assert "Import" in mapped["required_action"]
    assert mapped["diagnostic_reason"] == "session_missing"
    assert mapped["status_label"] != "Auth stale"
    assert "failed" not in mapped["status_label"].lower()


def test_disabled_mapping_no_auth_failure_primary():
    mapped = map_canonical_account(
        _row(
            account_id=13,
            classification="ACCOUNT_DISABLED",
            intended_operational_role="INTENTIONALLY_DISABLED",
            purpose="disabled",
            auth_valid=None,
            reason_codes=["role_intentionally_disabled"],
            probed=False,
        ),
        freshness=_fresh(),
    )
    assert mapped["display_status"] == "DISABLED"
    assert mapped["authorization_label"] == "Not checked"
    assert mapped["required_action"] == "None"
    assert "failed" not in mapped["status_label"].lower()
    assert "allowed" not in mapped["status_label"].lower()


def test_protected_and_reserved_mapping():
    protected = map_canonical_account(
        _row(
            account_id=110,
            classification="INTENTIONALLY_EXCLUDED",
            intended_operational_role="PROTECTED_CONTROLLER",
            auth_valid=None,
            probed=False,
        ),
        freshness=_fresh(),
    )
    reserved = map_canonical_account(
        _row(
            account_id=113,
            classification="INTENTIONALLY_EXCLUDED",
            intended_operational_role="AI_OR_INFRASTRUCTURE_RESERVED",
            auth_valid=None,
            probed=False,
        ),
        freshness=_fresh(),
    )
    assert protected["display_status"] == "PROTECTED"
    assert reserved["display_status"] == "RESERVED"
    assert protected["authorization_label"] == "Not applicable"
    assert reserved["authorization_label"] == "Not applicable"


def test_stale_probe_mapping_does_not_keep_ready():
    mapped = map_canonical_account(
        _row(account_id=108),
        freshness=_fresh(fresh=False, label="STALE_MATRIX"),
    )
    assert mapped["display_status"] == "CHECK_REQUIRED"
    assert mapped["authorization_label"] == "Probe expired"


def test_blocked_auth_failed_shows_concrete_reason():
    mapped = map_canonical_account(
        _row(
            classification="AUTH_FAILED",
            auth_valid=False,
            story_api_available=False,
            reason_codes=["telegram_unauthorized"],
            safe_error_summary="Telegram session is not authorized",
        ),
        freshness=_fresh(),
    )
    assert mapped["display_status"] == "BLOCKED"
    assert "Unauthorized" in mapped["status_detail"] or "not authorized" in mapped["status_detail"].lower()


def test_summary_totals_reconcile_with_canonical_matrix_fixture():
    matrix = {
        "accounts": [
            _row(account_id=106, classification="CERTIFIED_PUBLISH", intended_operational_role="CERTIFIED_REFERENCE"),
            _row(account_id=108),
            _row(
                account_id=107,
                classification="CONFIG_INCOMPLETE",
                auth_valid=None,
                session_present=False,
                reason_codes=["session_missing"],
            ),
            _row(
                account_id=13,
                classification="ACCOUNT_DISABLED",
                intended_operational_role="INTENTIONALLY_DISABLED",
                purpose="disabled",
                auth_valid=None,
                probed=False,
            ),
            _row(
                account_id=110,
                classification="INTENTIONALLY_EXCLUDED",
                intended_operational_role="PROTECTED_CONTROLLER",
                auth_valid=None,
                probed=False,
            ),
            _row(
                account_id=113,
                classification="INTENTIONALLY_EXCLUDED",
                intended_operational_role="AI_OR_INFRASTRUCTURE_RESERVED",
                auth_valid=None,
                probed=False,
            ),
        ]
    }
    views = build_operator_account_views(matrix, freshness=_fresh())
    summary = views["summary"]
    assert summary["authorized"] == 2
    assert summary["certified"] == 1
    assert summary["ready"] == 1
    assert summary["needs_session"] == 1
    assert summary["disabled"] == 1
    assert summary["unavailable"] == 3
    assert summary["needs_attention"] == 1
    assert summary["default_filter"] == "needs_attention"
    assert views["accounts_by_id"][107]["display_status"] == "NEEDS_SESSION"


def test_accounts_and_fleet_share_same_mapper_function():
    from src.dashboard import accounts_main_view, fleet_readiness_routes
    from src.stories import operator_account_presentation as mapper

    assert "build_operator_account_views" in Path(accounts_main_view.__file__).read_text(encoding="utf-8")
    assert "build_operator_account_views" in Path(fleet_readiness_routes.__file__).read_text(encoding="utf-8")
    assert callable(mapper.map_canonical_account)
    assert callable(mapper.build_operator_account_views)

def test_primary_table_rejects_contradictory_labels():
    dirty = "Technical Health: Needs Repair Governance: Allowed Story Runtime: Execution locked"
    clean = "Story Status Certified Authorization Auth OK Role Publishing Required Action None"
    assert primary_table_text_is_clean(clean)
    assert not primary_table_text_is_clean(dirty)


def test_live_canonical_matrix_expected_shape_if_present():
    path = Path("/opt/autostory/data/fleet-readiness/latest.json")
    if not path.is_file():
        return
    views = build_operator_account_views()
    summary = views["summary"]
    by_id = views["accounts_by_id"]
    assert summary["total_accounts"] == 104
    # Post account-107 recovery (2026-07-28): 94 authorized, 0 needs session.
    assert summary["authorized"] == 94
    assert summary["certified"] == 3
    assert summary["ready"] == 91
    assert summary["needs_session"] == 0
    assert summary["disabled"] == 5
    assert summary["unavailable"] == 10
    assert by_id[107]["display_status"] == "CERTIFIED"
    assert by_id[107]["authorization_label"] == "Auth OK"
    assert by_id[106]["display_status"] == "CERTIFIED"
    assert by_id[140]["display_status"] == "CERTIFIED"
    assert by_id[13]["display_status"] == "DISABLED"
    assert by_id[110]["display_status"] == "PROTECTED"
    assert by_id[113]["display_status"] == "RESERVED"
