"""Wave A — Legacy /api blueprint fail-closed auth (P0)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def app(monkeypatch):
    monkeypatch.setenv("DASHBOARD_ADMIN_TOKEN", "wave-a-test-token-not-for-prod")
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("SCHEDULER_MUTATIONS_ENABLED", "false")
    monkeypatch.setenv("MESSAGES_EXECUTION_ENABLED", "false")
    monkeypatch.setenv("SCHEDULED_DM_ENABLED", "false")
    # Avoid starting background workers during create_app
    monkeypatch.setenv("READINESS_WORKER_ENABLED", "false")
    monkeypatch.setenv("AI_AGENT_AUTO_LOOP_ENABLED", "false")

    from src.dashboard.app import create_app

    application = create_app()
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


def _auth_headers():
    return {"X-Admin-Token": "wave-a-test-token-not-for-prod"}


def test_wave_a_source_has_blueprint_before_request():
    src = (ROOT / "src/dashboard/routes.py").read_text(encoding="utf-8")
    assert "def require_legacy_api_authorization" in src
    assert "_LEGACY_API_PUBLIC_GET_PATHS" in src
    assert "dashboard_api_authorized" in (ROOT / "src/dashboard/auth_access.py").read_text()


def test_anonymous_accounts_denied(client):
    r = client.get("/api/accounts")
    assert r.status_code == 401
    assert r.get_json().get("error") == "unauthorized"


def test_anonymous_stats_denied(client):
    r = client.get("/api/stats")
    assert r.status_code == 401


def test_anonymous_status_mutation_denied(client):
    r = client.put(
        "/api/accounts/999999/status",
        json={"status": "active"},
        content_type="application/json",
    )
    assert r.status_code == 401


def test_anonymous_media_upload_denied(client):
    r = client.post(
        "/api/media/upload",
        data={},
        content_type="multipart/form-data",
    )
    assert r.status_code == 401


def test_public_health_still_anonymous(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.get_json().get("status") == "healthy"


def test_authed_accounts_allowed(client):
    r = client.get("/api/accounts", headers=_auth_headers())
    # May be 200 (empty list) — must not be 401
    assert r.status_code != 401
    assert r.status_code == 200


def test_authed_stats_allowed(client):
    r = client.get("/api/stats", headers=_auth_headers())
    assert r.status_code == 200
    body = r.get_json()
    assert "accounts" in body


def test_authed_media_upload_reaches_validation(client):
    """Auth passes; missing file still 400 (not 401)."""
    r = client.post(
        "/api/media/upload",
        headers=_auth_headers(),
        data={},
        content_type="multipart/form-data",
    )
    assert r.status_code == 400
    assert "No file" in (r.get_json() or {}).get("error", "")


def test_messages_api_still_independent_auth(client):
    r = client.get("/api/messages/status")
    assert r.status_code == 401
    r2 = client.get("/api/messages/status", headers=_auth_headers())
    assert r2.status_code == 200
