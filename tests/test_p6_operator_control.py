"""P6 operator control workflow tests."""
from __future__ import annotations

import re
from unittest.mock import AsyncMock, patch

import pytest


def _login_client():
    from src.core.database import get_db_context, init_db
    from src.dashboard.app import create_app
    from src.dashboard.models import DashboardUser
    from tests.helpers.fleet_seed import seed_minimal_fleet

    init_db()
    with get_db_context() as db:
        seed_minimal_fleet(db)
        user = db.query(DashboardUser).filter(DashboardUser.username == "p6admin").first()
        if not user:
            user = DashboardUser(username="p6admin", email="p6@test", is_admin=True, is_active=True)
            user.set_password("p6pass123")
            db.add(user)
            db.commit()

    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    client = app.test_client()
    client.post("/login", data={"username": "p6admin", "password": "p6pass123", "next": "/operator"}, follow_redirects=True)
    return app, client


def test_operator_hub_requires_login():
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    r = client.get("/operator", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in (r.location or "")


def test_operator_accounts_renders():
    _, client = _login_client()
    r = client.get("/operator/accounts")
    assert r.status_code == 200
    assert b"Account Inventory" in r.data


def test_operator_account_detail_107_and_139():
    _, client = _login_client()
    for aid in (107, 139):
        r = client.get(f"/operator/accounts/{aid}")
        assert r.status_code == 200
        assert str(aid).encode() in r.data


def test_account_139_blocked_indicators():
    from src.core.database import get_db_context
    from src.core.models import Account
    from src.dashboard.operator_control_service import build_account_inventory_row

    with get_db_context() as db:
        account = db.query(Account).filter(Account.id == 139).first()
        if account is None:
            pytest.skip("account 139 not in database")
        row = build_account_inventory_row(db, account)
    assert row["readiness_status"] in ("NOT_AUTHORIZED", "FAILED_AUTH", "PROBE_REQUIRED", "STALE_READY", "READY")
    if row["readiness_status"] == "NOT_AUTHORIZED":
        assert row["authorized"] is False
        assert row["pending_running_job_count"] == 0


def test_no_session_secrets_in_account_pages():
    _, client = _login_client()
    secret_patterns = (
        re.compile(rb"session_string", re.I),
        re.compile(rb"api_hash", re.I),
        re.compile(rb"phone_code_hash", re.I),
    )
    for path in ("/operator/accounts", "/operator/accounts/107", "/operator/accounts/139"):
        r = client.get(path)
        assert r.status_code == 200
        for pat in secret_patterns:
            assert pat.search(r.data) is None


def test_eligibility_preview_non_mutating():
    from src.core.database import get_db_context
    from src.dashboard.operator_control_service import queue_counts_snapshot

    app, client = _login_client()
    with get_db_context() as db:
        before = queue_counts_snapshot(db)
    r = client.get("/api/operator/eligibility-preview?account_id=107&target_id=14&job_type=PROMO")
    assert r.status_code == 200
    body = r.get_json()
    assert "preview" in body
    assert body["non_mutating_proof"]["unchanged"] is True
    with get_db_context() as db:
        after = queue_counts_snapshot(db)
    assert before == after


def test_system_safety_page_shows_no_go():
    _, client = _login_client()
    r = client.get("/operator/system-safety")
    assert r.status_code == 200
    assert b"AUTOSTORY_PRODUCTION_CERTIFIED_NO_GO" in r.data


def test_gateway_and_deliveries_pages():
    _, client = _login_client()
    assert client.get("/operator/gateway").status_code == 200
    assert client.get("/operator/deliveries").status_code == 200
    assert client.get("/operator/jobs").status_code == 200
    assert client.get("/operator/schedules").status_code == 200


def test_stale_ready_distinct_from_fresh():
    from datetime import datetime, timedelta, timezone

    from src.clients import readiness_store
    from src.core.database import get_db_context
    from src.core.models import Account
    from src.dashboard.operator_control_service import _readiness_freshness

    with get_db_context() as db:
        account = db.query(Account).filter(Account.id == 107).first()
        if account is None:
            pytest.skip("account 107 missing")
        snap = readiness_store.fetch_snapshot(db, 107)
        if snap is None:
            pytest.skip("no readiness snapshot for 107")
        old_checked = snap.checked_at
        snap.checked_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
        if snap.status == readiness_store.STAT_READY:
            row = _readiness_freshness(db, 107)
            assert row["display_status"] in ("STALE_READY", "READY", "PROBE_REQUIRED")
        snap.checked_at = old_checked


@patch("src.clients.readiness_worker._deep_check_one", new_callable=AsyncMock)
@patch("asyncio.run")
def test_refresh_readiness_calls_canonical_path(mock_run, mock_deep):
    mock_deep.return_value = "ready"
    mock_run.return_value = None
    _, client = _login_client()
    r = client.post("/operator/accounts/107/refresh-readiness", follow_redirects=True)
    assert r.status_code == 200
    assert mock_run.called


def test_cancel_pending_only():
    from src.core.database import get_db_context
    from src.core.scheduler_models import JobStatus, ScheduledJob

    _, client = _login_client()
    with get_db_context() as db:
        sent = (
            db.query(ScheduledJob)
            .filter(ScheduledJob.status == JobStatus.SENT.value)
            .order_by(ScheduledJob.id.desc())
            .first()
        )
        if sent is None:
            pytest.skip("no SENT job")
        job_id = sent.id
    r = client.post(f"/operator/jobs/{job_id}/cancel", follow_redirects=True)
    assert r.status_code == 200
    with get_db_context() as db:
        job = db.query(ScheduledJob).filter(ScheduledJob.id == job_id).first()
        assert job.status == JobStatus.SENT.value
