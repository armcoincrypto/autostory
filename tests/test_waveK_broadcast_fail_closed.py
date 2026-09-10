"""Wave K — Broadcast fail-closed certification contracts."""
from __future__ import annotations

import os
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_broadcast_guard_defaults_fail_closed(monkeypatch):
    from src.dashboard.broadcast_guard import broadcast_execution_enabled

    monkeypatch.delenv("BROADCAST_EXECUTION_ENABLED", raising=False)
    assert broadcast_execution_enabled() is False
    monkeypatch.setenv("BROADCAST_EXECUTION_ENABLED", "true")
    assert broadcast_execution_enabled() is True
    monkeypatch.setenv("BROADCAST_EXECUTION_ENABLED", "false")
    assert broadcast_execution_enabled() is False


def test_broadcast_routes_do_not_proxy_to_8015():
    src = (ROOT / "src/dashboard/broadcast_routes.py").read_text(encoding="utf-8")
    assert "ai_coding_api_base_url" not in src
    assert "fetch_upstream" not in src
    assert "broadcast_execution_disabled" in src
    assert 'url_prefix="/api/v1/broadcast"' in src


def test_broadcast_owner_page_fail_closed_copy():
    text = (ROOT / "src/dashboard/templates/broadcast.html").read_text(encoding="utf-8")
    assert "fail-closed" in text.lower() or "Not enabled" in text
    assert 'href="/messages"' in text
    assert "Save draft" not in text
    assert "tiny-cohort" not in text
    assert "bc-send-confirm" not in text


def test_broadcast_primary_nav_present():
    base = (ROOT / "src/dashboard/templates/base.html").read_text(encoding="utf-8")
    assert 'href="/broadcast"' in base
    assert "Broadcast" in base


def test_broadcast_api_auth_and_deny(monkeypatch):
    monkeypatch.setenv("DASHBOARD_ADMIN_TOKEN", "wavek-broadcast-token")
    monkeypatch.setenv("BROADCAST_EXECUTION_ENABLED", "false")
    monkeypatch.setenv("AI_AGENT_AUTO_LOOP_ENABLED", "false")
    # Avoid readiness worker noise
    monkeypatch.setenv("READINESS_WORKER_IN_WEB", "0")

    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    assert client.get("/api/v1/broadcast/status").status_code == 401
    assert client.get("/api/v1/broadcast/campaigns").status_code == 401
    assert client.post("/api/v1/broadcast/campaigns", json={"title": "x"}).status_code == 401

    headers = {"X-Admin-Token": "wavek-broadcast-token"}
    st = client.get("/api/v1/broadcast/status", headers=headers)
    assert st.status_code == 200
    body = st.get_json()
    assert body["execution_enabled"] is False
    assert body["mode"] == "fail_closed"

    listed = client.get("/api/v1/broadcast/campaigns", headers=headers)
    assert listed.status_code == 200
    assert listed.get_json() == []

    created = client.post(
        "/api/v1/broadcast/campaigns",
        headers=headers,
        json={"title": "should-deny", "channel": "telegram", "message_body": "hi"},
    )
    assert created.status_code == 403
    assert created.get_json()["error"] == "broadcast_execution_disabled"

    send = client.post(
        "/api/v1/broadcast/campaigns/abc/send/tiny-cohort",
        headers=headers,
        json={"confirmation_text": "SEND"},
    )
    assert send.status_code == 403

    page = client.get("/broadcast", headers=headers)
    # page auth may redirect without session; admin token accepted via dashboard_api_authorized
    assert page.status_code in {200, 302}
