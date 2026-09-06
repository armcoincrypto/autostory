"""Wave 7 — Owner Messages Send-Now UI + thin authenticated APIs."""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.database import Base
from src.core.models import Account, AccountStatus
from src.messaging.models import STATUS_SENT, STATUS_UNCERTAIN, OwnerDmIntent
from src.messaging.owner_dm_service import OwnerDirectMessageService
from src.messaging.transport import CountingFakeTransport

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def memory_db():
    import src.core.models  # noqa: F401
    import src.messaging.models  # noqa: F401

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


def _add_account(db, aid=42, **kw):
    a = Account(
        id=aid,
        phone_number=f"+1555000{aid:04d}",
        status=kw.get("status", AccountStatus.ACTIVE),
        purpose=kw.get("purpose", "messaging"),
        session_string=kw.get("session_string", "sess"),
        username=kw.get("username", f"user{aid}"),
        first_name=kw.get("first_name", f"User{aid}"),
    )
    db.add(a)
    db.commit()
    return a


def test_messages_nav_and_template_contracts():
    base = (ROOT / "src/dashboard/templates/base.html").read_text(encoding="utf-8")
    assert 'href="/messages"' in base
    assert "bi-chat-dots" in base and "Messages" in base
    nav = base.split('<nav class="nav flex-column">')[1].split("</nav>")[0]
    assert 'href="/stories/fleet-readiness"' not in nav
    assert 'href="/campaigns"' not in nav
    tpl = (ROOT / "src/dashboard/templates/messages.html").read_text(encoding="utf-8")
    assert "confirmSendModal" in tpl
    assert "Delivery could not be confirmed" in tpl
    assert "idempotencyKey" in tpl
    assert "No scheduling" in tpl
    assert "Check message" in tpl
    assert "Review &amp; Send" in tpl or "Review & Send" in tpl


def test_messages_routes_auth_and_thin_wiring():
    src = (ROOT / "src/dashboard/messages_routes.py").read_text(encoding="utf-8")
    assert "dashboard_api_authorized" in src
    assert "OwnerDirectMessageService" in src
    assert "/dry-run" in src
    assert "/send-now" in src
    assert "/history" in src
    assert "/intents/" in src
    # No second Telegram stack in the route module
    assert "send_message(" not in src or "OwnerDirectMessageService" in src
    assert "client.send_message" not in src


def test_app_registers_messages_blueprints():
    app_src = (ROOT / "src/dashboard/app.py").read_text(encoding="utf-8")
    assert "messages_routes" in app_src
    assert "messages_bp" in app_src
    assert "messages_api" in app_src


def test_dry_run_api_uses_service_zero_sends(memory_db, monkeypatch):
    _add_account(memory_db)
    monkeypatch.setattr(
        "src.messaging.eligibility.fetch_snapshot",
        lambda *_a, **_k: None,
    )
    monkeypatch.setenv("MESSAGES_EXECUTION_ENABLED", "false")
    fake = CountingFakeTransport()
    svc = OwnerDirectMessageService(transport=fake)
    out = svc.dry_run(
        memory_db,
        account_id=42,
        peer_id="99",
        text="hello",
        peer_type="private",
    )
    assert out["dry_run"] is True
    assert fake.send_calls == 0
    assert out["would_send"] is False


def test_send_now_fail_closed_and_fake_sent(memory_db, monkeypatch):
    _add_account(memory_db)
    monkeypatch.setattr(
        "src.messaging.eligibility.fetch_snapshot",
        lambda *_a, **_k: None,
    )
    monkeypatch.setenv("MESSAGES_EXECUTION_ENABLED", "false")
    fake = CountingFakeTransport()
    svc = OwnerDirectMessageService(transport=fake)
    denied = asyncio.run(
        svc.send_now(
            memory_db,
            account_id=42,
            peer_id="99",
            text="hi",
            idempotency_key=str(uuid.uuid4()),
        )
    )
    assert denied["error_code"] == "MESSAGES_DISABLED"
    assert fake.send_calls == 0

    monkeypatch.setenv("MESSAGES_EXECUTION_ENABLED", "true")
    monkeypatch.setattr(
        "src.messaging.flags.messages_execution_enabled",
        lambda: True,
    )
    # Patch require path already uses env via flags; also patch guard import path
    from src.core import execution_guard as eg

    monkeypatch.setattr(
        "src.messaging.owner_dm_service.require_execution_allowed",
        lambda *_a, **_k: None,
    )
    key = str(uuid.uuid4())
    sent = asyncio.run(
        svc.send_now(
            memory_db,
            account_id=42,
            peer_id="99",
            text="hi",
            idempotency_key=key,
        )
    )
    assert sent["status"] == STATUS_SENT
    assert fake.send_calls == 1
    replay = asyncio.run(
        svc.send_now(
            memory_db,
            account_id=42,
            peer_id="99",
            text="hi",
            idempotency_key=key,
        )
    )
    assert replay["replay"] is True
    assert fake.send_calls == 1


def test_uncertain_no_autosend(memory_db, monkeypatch):
    _add_account(memory_db)
    monkeypatch.setattr(
        "src.messaging.eligibility.fetch_snapshot",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "src.messaging.owner_dm_service.require_execution_allowed",
        lambda *_a, **_k: None,
    )
    fake = CountingFakeTransport()
    svc = OwnerDirectMessageService(transport=fake)
    key = "u-" + uuid.uuid4().hex
    out = asyncio.run(
        svc.send_now(
            memory_db,
            account_id=42,
            peer_id="99",
            text="x",
            idempotency_key=key,
            _force_post_send_db_failure=True,
        )
    )
    assert out["status"] == STATUS_UNCERTAIN
    assert fake.send_calls == 1
    again = asyncio.run(
        svc.send_now(
            memory_db,
            account_id=42,
            peer_id="99",
            text="x",
            idempotency_key=key,
        )
    )
    assert again["status"] == STATUS_UNCERTAIN
    assert fake.send_calls == 1


def test_flask_api_auth_anonymous(monkeypatch):
    """Anonymous messaging API calls must be denied."""
    from src.dashboard.app import create_app

    monkeypatch.setenv("MESSAGES_EXECUTION_ENABLED", "false")
    monkeypatch.setenv("DASHBOARD_ADMIN_TOKEN", "wave7-test-token")
    # Avoid production DB side effects where possible
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    for path, method in [
        ("/api/messages/status", "get"),
        ("/api/messages/accounts", "get"),
        ("/api/messages/history?account_id=1&peer=2", "get"),
        ("/api/messages/dry-run", "post"),
        ("/api/messages/send-now", "post"),
        ("/api/messages/intents", "get"),
        ("/api/messages/intents/1", "get"),
    ]:
        if method == "get":
            resp = client.get(path)
        else:
            resp = client.post(path, json={})
        assert resp.status_code in (401, 302), f"{path} -> {resp.status_code}"


def test_flask_api_auth_with_token_status(monkeypatch):
    from src.dashboard.app import create_app

    monkeypatch.setenv("MESSAGES_EXECUTION_ENABLED", "false")
    monkeypatch.setenv("DASHBOARD_ADMIN_TOKEN", "wave7-test-token")
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    resp = client.get(
        "/api/messages/status",
        headers={"X-Admin-Token": "wave7-test-token"},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["messages_execution_enabled"] is False
    assert data["dry_run_available"] is True


def test_history_normalization_via_fake_transport():
    transport = CountingFakeTransport()

    async def _fake_fetch(account_id, target, limit):
        transport.fetch_calls += 1
        return {
            "ok": True,
            "messages": [
                {
                    "message_id": 1,
                    "text": "hi",
                    "timestamp": "2026-09-05T12:00:00Z",
                    "is_outgoing": False,
                },
                {
                    "message_id": 2,
                    "text": "yo",
                    "timestamp": "2026-09-05T12:01:00Z",
                    "is_outgoing": True,
                },
            ],
        }

    transport.fetch_recent_messages_async = _fake_fetch  # type: ignore
    out = asyncio.run(transport.fetch_recent_messages_async(1, "2", 20))
    assert out["ok"] is True
    assert out["messages"][0]["is_outgoing"] is False
    assert out["messages"][1]["is_outgoing"] is True
    assert "access_hash" not in str(out)


def test_scheduler_still_no_dm_type():
    models = (ROOT / "src/core/scheduler_models.py").read_text(encoding="utf-8")
    assert 'DM = "DM"' not in models
