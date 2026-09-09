"""Wave E — Accounts owner health truth from canonical fleet matrix."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.dashboard.accounts_owner_health import (
    attach_owner_health,
    build_owner_health_index,
    owner_health_payload_for_presentation,
)
from src.stories.operator_account_presentation import (
    map_canonical_account,
    summarize_operator_accounts,
)

ROOT = Path(__file__).resolve().parents[1]


def _fresh(**overrides):
    base = {
        "present": True,
        "fresh": True,
        "label": "FRESH_CACHED",
        "generated_at": "2026-09-09T15:58:38.577322Z",
        "source_audit_completed_at": "2026-09-09T15:58:38.428109Z",
        "ttl_hours": 24,
        "canonical_source": "/opt/autostory/data/fleet-readiness/latest.json",
    }
    base.update(overrides)
    return base


def _matrix_row(**overrides):
    base = {
        "account_id": 106,
        "display_name": "Fixture",
        "phone_redacted": "+18***0106",
        "intended_operational_role": "CERTIFIED_REFERENCE",
        "classification": "CERTIFIED_PUBLISH",
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
        "controlled_publish_certified": True,
        "last_auth_at": "2026-09-09T15:58:00Z",
    }
    base.update(overrides)
    return base


def test_wave_e_source_contracts():
    main = (ROOT / "src/dashboard/accounts_main_view.py").read_text(encoding="utf-8")
    assert "build_operator_account_views" in main
    assert "owner_health" in main
    assert "getattr(account, \"health_status\"" not in main
    assert "account.health_status" not in main
    assert "TelegramClient" not in main
    tpl = (ROOT / "src/dashboard/templates/accounts_main.html").read_text(encoding="utf-8")
    assert "{{ account.status_label }}" in tpl
    assert "account.health_status" not in tpl
    assert "data-filter-group=\"{{ account.filter_group }}\"" in tpl
    api = (ROOT / "src/dashboard/app.py").read_text(encoding="utf-8")
    assert "attach_owner_health" in api
    assert "build_owner_health_index" in api
    assert 'request.args.get("limit", 500' in api


def test_certified_vs_raw_error_orm_contradiction():
    """raw health_status=error + matrix CERTIFIED → owner Certified."""
    mapped = map_canonical_account(_matrix_row(account_id=106), freshness=_fresh())
    assert mapped["display_status"] == "CERTIFIED"
    assert mapped["status_label"] == "Certified"
    assert mapped["filter_group"] == "ready"
    row = {"id": 106, "health_status": "error", "general_health_label": "Unknown"}
    # Simulate index with this presentation
    index = {
        "by_id": {106: mapped},
        "matrix_fresh": True,
        "summary": {},
    }
    attach_owner_health(row, index=index)
    assert row["owner_health"] == "CERTIFIED"
    assert row["owner_health_label"] == "Certified"
    assert row["owner_filter_group"] == "ready"
    assert row["raw_health_status"] == "error"
    assert row["health_status"] == "error"  # preserved
    assert row["general_health_label"] == "Unknown"  # legacy field untouched


def test_auth_failed_maps_blocked_needs_attention():
    mapped = map_canonical_account(
        _matrix_row(
            account_id=99,
            classification="AUTH_FAILED",
            auth_valid=False,
            controlled_publish_certified=False,
        ),
        freshness=_fresh(),
    )
    assert mapped["display_status"] == "BLOCKED"
    assert mapped["filter_group"] == "needs_attention"
    assert mapped["authorization_label"] in {"Needs login/session", "Not ready"}


def test_disabled_and_excluded_map():
    disabled = map_canonical_account(
        _matrix_row(
            account_id=13,
            classification="ACCOUNT_DISABLED",
            intended_operational_role="INTENTIONALLY_DISABLED",
            auth_valid=False,
            controlled_publish_certified=False,
        ),
        freshness=_fresh(),
    )
    assert disabled["display_status"] == "DISABLED"
    assert disabled["filter_group"] == "unavailable"

    reserved = map_canonical_account(
        _matrix_row(
            account_id=110,
            classification="INTENTIONALLY_EXCLUDED",
            intended_operational_role="AI_OR_INFRASTRUCTURE_RESERVED",
            auth_valid=False,
            controlled_publish_certified=False,
        ),
        freshness=_fresh(),
    )
    assert reserved["display_status"] == "RESERVED"
    assert reserved["filter_group"] == "unavailable"


def test_stale_matrix_fail_closed_certified():
    stale = _fresh(fresh=False, label="STALE")
    mapped = map_canonical_account(_matrix_row(account_id=106), freshness=stale)
    assert mapped["display_status"] == "CHECK_REQUIRED"
    assert mapped["filter_group"] == "needs_attention"


def test_missing_matrix_row_check_required():
    payload = owner_health_payload_for_presentation(None)
    assert payload["owner_health"] == "CHECK_REQUIRED"
    assert payload["owner_filter_group"] == "needs_attention"


def test_filters_align_with_row_status():
    rows = [
        map_canonical_account(_matrix_row(account_id=1), freshness=_fresh()),
        map_canonical_account(
            _matrix_row(account_id=2, classification="AUTH_FAILED", auth_valid=False),
            freshness=_fresh(),
        ),
        map_canonical_account(
            _matrix_row(
                account_id=3,
                classification="ACCOUNT_DISABLED",
                intended_operational_role="INTENTIONALLY_DISABLED",
            ),
            freshness=_fresh(),
        ),
    ]
    for r in rows:
        if r["display_status"] in {"CERTIFIED", "READY"}:
            assert r["filter_group"] == "ready"
        elif r["display_status"] in {"BLOCKED", "NEEDS_SESSION", "CHECK_REQUIRED"}:
            assert r["filter_group"] == "needs_attention"
        elif r["display_status"] in {"DISABLED", "PROTECTED", "RESERVED"}:
            assert r["filter_group"] == "unavailable"
    summary = summarize_operator_accounts(rows, freshness=_fresh())
    assert summary["certified"] == 1
    assert summary["needs_attention"] == 1
    assert summary["unavailable"] == 1


def test_accounts_main_context_ignores_orm_health_error(monkeypatch):
    import src.dashboard.accounts_main_view as amv

    matrix = {
        "generated_at": "2026-09-09T15:58:38.577322Z",
        "accounts": [
            _matrix_row(account_id=106),
            _matrix_row(
                account_id=99,
                classification="AUTH_FAILED",
                auth_valid=False,
                controlled_publish_certified=False,
            ),
        ],
        "totals": {},
    }
    freshness = _fresh()

    monkeypatch.setattr(amv, "load_latest_matrix", lambda: matrix)
    monkeypatch.setattr(amv, "matrix_freshness", lambda _m: freshness)
    monkeypatch.setattr(amv, "list_pinned_account_ids", lambda _db: [])
    monkeypatch.setattr(amv, "_batch_active_jobs", lambda _db: {})
    monkeypatch.setattr(amv, "build_eligibility_preview_safe", lambda _db, module="stories": amv.empty_eligibility_preview(module))
    monkeypatch.setattr(amv, "scheduler_mutations_enabled", lambda: False)
    monkeypatch.setattr(amv, "campaign_execution_enabled", lambda: False)

    acc106 = SimpleNamespace(
        id=106,
        username="u106",
        phone_number="+100106",
        first_name="A",
        last_name="",
        status=SimpleNamespace(value="active"),
        purpose="both",
        health_status="error",
        proxy_config=None,
        flood_wait_until=None,
        stories_today=0,
        last_story_success_at=None,
        user_id=100106,
    )
    acc99 = SimpleNamespace(
        id=99,
        username="u99",
        phone_number="+100099",
        first_name="B",
        last_name="",
        status=SimpleNamespace(value="active"),
        purpose="both",
        health_status="error",
        proxy_config=None,
        flood_wait_until=None,
        stories_today=0,
        last_story_success_at=None,
        user_id=100099,
    )
    db = MagicMock()
    q = MagicMock()
    q.order_by.return_value.all.return_value = [acc106, acc99]
    db.query.return_value = q

    ctx = amv.build_accounts_main_context(db)
    by = {r["id"]: r for r in ctx["accounts"]}
    assert by[106]["owner_health"] == "CERTIFIED"
    assert by[106]["status_label"] == "Certified"
    assert by[106]["filter_group"] == "ready"
    assert by[99]["owner_health"] == "BLOCKED"
    assert by[99]["filter_group"] == "needs_attention"
    assert ctx["page_perf"]["external_telegram_calls_on_page_load"] == 0
    assert ctx["operator_summary"]["certified"] == 1
    assert ctx["operator_summary"]["needs_attention"] == 1


def test_attach_owner_health_uses_live_index_when_present(monkeypatch):
    mapped = map_canonical_account(_matrix_row(account_id=106), freshness=_fresh())
    monkeypatch.setattr(
        "src.dashboard.accounts_owner_health.build_operator_account_views",
        lambda matrix=None, freshness=None: {
            "accounts_by_id": {106: mapped},
            "summary": {"certified": 1, "total_accounts": 1, "needs_attention": 0, "ready": 0, "authorized": 1, "blocked": 0, "check_required": 0, "disabled": 0, "unavailable": 0},
            "freshness": _fresh(),
            "canonical_source": "test",
        },
    )
    idx = build_owner_health_index()
    row = attach_owner_health({"id": 106, "health_status": "error"}, index=idx)
    assert row["owner_health"] == "CERTIFIED"
    assert row["raw_health_status"] == "error"
