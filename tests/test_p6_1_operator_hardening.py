"""P6.1 operator hardening tests."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest


def _login_client():
    from src.core.database import get_db_context, init_db
    from src.dashboard.app import create_app
    from src.dashboard.models import DashboardUser

    init_db()
    with get_db_context() as db:
        user = db.query(DashboardUser).filter(DashboardUser.username == "p61admin").first()
        if not user:
            user = DashboardUser(username="p61admin", email="p61@test", is_admin=True, is_active=True)
            user.set_password("p61pass123")
            db.add(user)
            db.commit()

    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    client = app.test_client()
    client.post("/login", data={"username": "p61admin", "password": "p61pass123"}, follow_redirects=True)
    return app, client


def test_build_info_endpoint():
    _, client = _login_client()
    r = client.get("/api/operator/build-info")
    assert r.status_code == 200
    body = r.get_json()
    assert "git_commit" in body
    assert body.get("production_no_go") is True


def test_schedule_next_occurrence_and_same_day_duplicate():
    from src.core.database import get_db_context
    from src.core.scheduler_models import AccountTargetBinding, ScheduleProfile, ScheduleRule
    from src.dashboard.operator_control_service import (
        build_schedule_eligibility_rows,
        compute_next_occurrence,
        same_day_duplicate_status,
    )

    with get_db_context() as db:
        profile = db.query(ScheduleProfile).filter(ScheduleProfile.is_enabled.is_(True)).first()
        if profile is None:
            pytest.skip("no enabled schedule profile")
        rule = (
            db.query(ScheduleRule)
            .filter(ScheduleRule.account_id == profile.account_id, ScheduleRule.is_enabled.is_(True))
            .first()
        )
        if rule is None:
            pytest.skip("no enabled schedule rule")
        nxt = compute_next_occurrence(rule, profile)
        assert "available" in nxt
        binding = (
            db.query(AccountTargetBinding)
            .filter(AccountTargetBinding.account_id == profile.account_id)
            .first()
        )
        if binding:
            dup = same_day_duplicate_status(
                db,
                account_id=profile.account_id,
                target_id=binding.target_id,
                job_type=rule.type,
                timezone_name=profile.timezone,
            )
            assert "exists" in dup
            assert "jobs" in dup
        rows = build_schedule_eligibility_rows(db)
        assert rows
        assert "next_occurrence" in rows[0]
        assert "same_day_duplicate" in rows[0]


@patch("src.dashboard.operator_control_service._lookup_message_by_body", new_callable=AsyncMock)
def test_reconciliation_no_send(mock_lookup):
    from src.core.database import get_db_context
    from src.core.p5d_gateway_reconciliation import AMBIGUOUS_RECONCILIATION_REQUIRED
    from src.core.scheduler_models import MessageDelivery
    from src.dashboard.operator_control_service import queue_counts_snapshot, run_delivery_reconciliation_check

    mock_lookup.return_value = None
    with get_db_context() as db:
        delivery = (
            db.query(MessageDelivery)
            .filter(MessageDelivery.id == 147)
            .first()
        )
        if delivery is None:
            pytest.skip("delivery 147 missing")
        before = queue_counts_snapshot(db)

    with get_db_context() as db:
        import asyncio

        result = asyncio.run(run_delivery_reconciliation_check(db, 147))
    assert result.get("mutated") is False
    assert result.get("outcome") in (
        AMBIGUOUS_RECONCILIATION_REQUIRED,
        "NOT_SENT_CONFIRMED",
        "SENT_CONFIRMED",
    )
    with get_db_context() as db:
        after = queue_counts_snapshot(db)
    assert before == after


def test_reconciliation_post_requires_auth():
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    client = app.test_client()
    r = client.post("/operator/deliveries/147/reconcile", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in (r.location or "")


@patch("src.clients.membership_check.check_targets_membership_sequential", new_callable=AsyncMock)
@patch("src.clients.manager.client_manager.add_account", new_callable=AsyncMock)
@patch("src.clients.manager.client_manager.remove_account", new_callable=AsyncMock)
def test_binding_refresh_non_mutation(mock_remove, mock_add, mock_check):
    from src.core.database import get_db_context
    from src.core.scheduler_models import AccountTargetBinding
    from src.dashboard.operator_control_service import queue_counts_snapshot, refresh_binding_permission

    mock_add.return_value = (object(), None)
    mock_remove.return_value = None
    with get_db_context() as db:
        binding = db.query(AccountTargetBinding).first()
        if binding is None:
            pytest.skip("no bindings")
        mock_check.return_value = [
            {
                "target_id": int(binding.target_id),
                "status": "joined",
                "can_post": True,
                "message": "ok",
            }
        ]
        before = queue_counts_snapshot(db)
        import asyncio

        result = asyncio.run(refresh_binding_permission(db, int(binding.id)))
        after = queue_counts_snapshot(db)
    assert result.get("ok") is True
    assert result["non_mutating_proof"]["execution_unchanged"] is True
    assert before == after


def test_proxy_aware_external_redirect_behavior():
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    r = client.get("/operator", base_url="https://ex.zellotex.com", follow_redirects=False)
    assert r.status_code == 302
    loc = r.location or ""
    assert "/login" in loc
    assert "127.0.0.1" not in loc


def test_schedules_page_renders_new_columns():
    _, client = _login_client()
    r = client.get("/operator/schedules")
    assert r.status_code == 200
    assert b"Same-day dup" in r.data
    assert b"Next" in r.data


def test_system_safety_shows_build_info():
    _, client = _login_client()
    r = client.get("/operator/system-safety")
    assert r.status_code == 200
    assert b"git_commit" in r.data
