"""Wave 7D — Recent Messages panel + clearer blocked-account error codes."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.database import Base
from src.core.models import Account, AccountStatus
from src.messaging.eligibility import DmEligibility
from src.messaging.models import (
    STATUS_FAILED,
    STATUS_SENT,
    STATUS_UNCERTAIN,
    OwnerDmIntent,
)
from src.messaging.owner_dm_service import (
    OwnerDirectMessageService,
    owner_message_for_eligibility,
    owner_message_for_guard_deny,
)
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


def test_owner_error_mapping_codes():
    assert owner_message_for_eligibility("PROTECTED")[0] == "ACCOUNT_PROTECTED"
    assert owner_message_for_eligibility("RESERVED")[0] == "ACCOUNT_RESERVED"
    assert owner_message_for_eligibility("DISABLED")[0] == "ACCOUNT_DISABLED"
    assert owner_message_for_eligibility("AUTH_FAILED")[0] == "AUTH_REQUIRED"
    assert "disabled" in owner_message_for_eligibility("DISABLED")[1].lower()

    kill = SimpleNamespace(
        reason_code="messages_execution_disabled",
        blockers=["messages_execution_disabled"],
        message="off",
    )
    assert owner_message_for_guard_deny(kill) == (
        "MESSAGES_DISABLED",
        "Message sending is currently disabled.",
    )
    prot = SimpleNamespace(
        reason_code="account_governance_block",
        blockers=["account_protected"],
        message="Account is protected, held, or AI-reserved.",
    )
    code, msg = owner_message_for_guard_deny(prot)
    assert code == "ACCOUNT_PROTECTED"
    assert code != "MESSAGES_DISABLED"
    assert "protected" in msg.lower()

    reserved = SimpleNamespace(
        reason_code="account_governance_block",
        blockers=["account_ai_reserved"],
        message="x",
    )
    assert owner_message_for_guard_deny(reserved)[0] == "ACCOUNT_RESERVED"


def test_send_now_maps_eligibility_not_messages_disabled(memory_db, monkeypatch):
    _add_account(memory_db)
    monkeypatch.setattr(
        "src.messaging.eligibility.fetch_snapshot",
        lambda *_a, **_k: None,
    )
    monkeypatch.setenv("MESSAGES_EXECUTION_ENABLED", "true")
    monkeypatch.setattr(
        "src.messaging.owner_dm_service.require_execution_allowed",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "src.messaging.owner_dm_service.evaluate_dm_account_eligibility",
        lambda *_a, **_k: DmEligibility(
            False, "PROTECTED", "Account is protected from messaging."
        ),
    )
    fake = CountingFakeTransport()
    svc = OwnerDirectMessageService(transport=fake)
    out = asyncio.run(
        svc.send_now(
            memory_db,
            account_id=42,
            peer_id="99",
            text="nope",
            idempotency_key=str(uuid.uuid4()),
        )
    )
    assert out["error_code"] == "ACCOUNT_PROTECTED"
    assert out["error_code"] != "MESSAGES_DISABLED"
    assert fake.send_calls == 0
    assert "protected" in (out["error_message"] or "").lower()


def test_send_now_kill_switch_still_messages_disabled(memory_db, monkeypatch):
    _add_account(memory_db)
    monkeypatch.setattr(
        "src.messaging.eligibility.fetch_snapshot",
        lambda *_a, **_k: None,
    )
    monkeypatch.setenv("MESSAGES_EXECUTION_ENABLED", "false")
    fake = CountingFakeTransport()
    svc = OwnerDirectMessageService(transport=fake)
    out = asyncio.run(
        svc.send_now(
            memory_db,
            account_id=42,
            peer_id="99",
            text="hi",
            idempotency_key=str(uuid.uuid4()),
        )
    )
    assert out["error_code"] == "MESSAGES_DISABLED"
    assert fake.send_calls == 0


def test_recent_intents_api_auth_and_safe_fields(monkeypatch):
    from src.dashboard.app import create_app

    monkeypatch.setenv("MESSAGES_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("DASHBOARD_ADMIN_TOKEN", "wave7d-test-token")
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    anon = client.get("/api/messages/intents")
    assert anon.status_code in (401, 302)

    # Patch DB context used by the route to an in-memory session with sample intents.
    engine = create_engine("sqlite:///:memory:")
    import src.core.models  # noqa: F401
    import src.messaging.models  # noqa: F401

    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    db.add(
        Account(
            id=106,
            phone_number="+1000106",
            status=AccountStatus.ACTIVE,
            purpose="both",
            session_string="s",
            first_name="Rachael",
        )
    )
    now = datetime.utcnow()
    for status, mid, err in [
        (STATUS_SENT, 15, None),
        (STATUS_FAILED, None, "PEER_INVALID"),
        (STATUS_UNCERTAIN, 99, "UNCERTAIN"),
    ]:
        db.add(
            OwnerDmIntent(
                idempotency_key=f"k-{status}-{uuid.uuid4().hex[:8]}",
                account_id=106,
                peer_id="8531893204",
                peer_type="private",
                message_hash="abc",
                message_preview="preview",
                status=status,
                telegram_message_id=mid,
                error_code=err,
                created_at=now,
                sent_at=now if status == STATUS_SENT else None,
            )
        )
    db.commit()

    class _Ctx:
        def __enter__(self):
            return db

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        "src.dashboard.messages_routes.get_db_context",
        lambda: _Ctx(),
    )

    resp = client.get(
        "/api/messages/intents?limit=20",
        headers={"X-Admin-Token": "wave7d-test-token"},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["limit"] == 20
    assert len(data["intents"]) == 3
    statuses = {r["status"] for r in data["intents"]}
    assert STATUS_SENT in statuses
    assert STATUS_FAILED in statuses
    assert STATUS_UNCERTAIN in statuses
    row = data["intents"][0]
    for key in (
        "intent_id",
        "created_at",
        "account_id",
        "peer",
        "peer_display",
        "status",
    ):
        assert key in row
    blob = str(data)
    assert "session_string" not in blob
    assert "access_hash" not in blob
    assert "auth_key" not in blob
    # Bounded
    resp2 = client.get(
        "/api/messages/intents?limit=2",
        headers={"X-Admin-Token": "wave7d-test-token"},
    )
    assert len(resp2.get_json()["intents"]) == 2
    db.close()


def test_template_has_recent_panel_and_clear_errors():
    tpl = (ROOT / "src/dashboard/templates/messages.html").read_text(encoding="utf-8")
    assert "Recent Messages" in tpl
    assert "/api/messages/intents" in tpl
    assert "btn-refresh-recent" in tpl
    assert "ACCOUNT_PROTECTED" in tpl
    assert "loadRecentIntents" in tpl
    src = (ROOT / "src/dashboard/messages_routes.py").read_text(encoding="utf-8")
    assert 'route("/intents"' in src or "route('/intents'" in src
    assert "TelegramDmTransport" in src  # history still uses transport
    # list endpoint must not call telethon runtime
    assert "messages_intents_recent" in src


def test_eligible_dry_run_unchanged(memory_db, monkeypatch):
    _add_account(memory_db)
    monkeypatch.setattr(
        "src.messaging.eligibility.fetch_snapshot",
        lambda *_a, **_k: None,
    )
    monkeypatch.setenv("MESSAGES_EXECUTION_ENABLED", "true")
    fake = CountingFakeTransport()
    svc = OwnerDirectMessageService(transport=fake)
    out = svc.dry_run(
        memory_db, account_id=42, peer_id="99", text="hello", peer_type="private"
    )
    assert out["dry_run"] is True
    assert out["would_send"] is True
    assert fake.send_calls == 0
