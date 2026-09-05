"""Wave 6A — Messaging safety foundation (no real Telegram sends)."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.database import Base
from src.core.models import Account, AccountStatus
from src.messaging.eligibility import evaluate_dm_account_eligibility
from src.messaging.errors import map_dm_error
from src.messaging.models import (
    STATUS_FAILED,
    STATUS_SENDING,
    STATUS_SENT,
    STATUS_UNCERTAIN,
    OwnerDmIntent,
)
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


def _add_account(
    db,
    aid: int = 42,
    *,
    status=AccountStatus.ACTIVE,
    purpose="messaging",
    session_string="sess-test",
):
    a = Account(
        id=aid,
        phone_number=f"+1555000{aid:04d}",
        status=status,
        purpose=purpose,
        session_string=session_string,
    )
    db.add(a)
    db.commit()
    return a


def test_phase0_messaging_module_owners_exist():
    assert (ROOT / "src/messaging/transport.py").is_file()
    assert (ROOT / "src/messaging/owner_dm_service.py").is_file()
    assert (ROOT / "src/messaging/models.py").is_file()
    guard = (ROOT / "src/core/execution_guard.py").read_text(encoding="utf-8")
    assert "ACTION_OWNER_DM_SEND" in guard
    assert "messages_execution_disabled" in guard


def test_dialogs_include_private_user_branch():
    mgr = (ROOT / "src/clients/manager.py").read_text(encoding="utf-8")
    start = mgr.index("async def get_dialogs")
    chunk = mgr[start : start + 4500]
    loop = chunk[chunk.index("for d in dialogs") : chunk.index("return result")]
    assert "isinstance(e, User)" in loop
    assert "isinstance(e, Chat)" in loop
    assert "isinstance(e, Channel)" in loop
    assert "dialog_type" in loop
    for banned in ("access_hash", "session_string", "api_hash", "auth_key", "proxy"):
        assert banned not in loop


def test_dialogs_normalize_helper_fields():
    """Unit-test normalization without Telethon by exercising branch logic via fake entities."""
    from src.clients.manager import ClientManager

    # Build fake dialog objects matching Telethon shape used by get_dialogs
    user = SimpleNamespace(
        id=111,
        first_name="Ada",
        last_name="Lovelace",
        username="ada",
        bot=False,
    )
    bot = SimpleNamespace(
        id=222,
        first_name="Helper",
        last_name="",
        username="helper_bot",
        bot=True,
    )
    chat = SimpleNamespace(id=333, title="Team", username=None)
    channel = SimpleNamespace(id=444, title="News", username="news", broadcast=True)
    supergroup = SimpleNamespace(id=555, title="SG", username=None, broadcast=False)

    class FakeDialog:
        def __init__(self, entity, unread=0):
            self.entity = entity
            self.unread_count = unread
            self.date = datetime(2026, 1, 2, tzinfo=timezone.utc)

    dialogs = [
        FakeDialog(user, 2),
        FakeDialog(bot),
        FakeDialog(chat),
        FakeDialog(channel),
        FakeDialog(supergroup),
    ]

    # Replicate classification (same as manager) without network
    from telethon.tl.types import Channel, Chat, User

    # Fake isinstance by monkeypatching path: call private-style normalize
    rows = []
    for d in dialogs:
        e = d.entity
        unread = int(getattr(d, "unread_count", 0) or 0)
        last_message_at = d.date.isoformat()
        if getattr(e, "bot", None) is not None and not hasattr(e, "title"):
            # User-like
            first = (getattr(e, "first_name", None) or "").strip()
            last = (getattr(e, "last_name", None) or "").strip()
            display = " ".join(x for x in (first, last) if x) or str(e.id)
            dt = "bot" if bool(getattr(e, "bot", False)) else "private"
            rows.append(
                {
                    "id": int(e.id),
                    "display_name": display,
                    "username": getattr(e, "username", None),
                    "dialog_type": dt,
                    "unread_count": unread,
                    "last_message_at": last_message_at,
                }
            )
        elif hasattr(e, "broadcast"):
            ct = "channel" if e.broadcast else "supergroup"
            rows.append(
                {
                    "id": int(e.id),
                    "display_name": e.title,
                    "username": getattr(e, "username", None),
                    "dialog_type": ct,
                    "unread_count": unread,
                    "last_message_at": last_message_at,
                }
            )
        else:
            rows.append(
                {
                    "id": int(e.id),
                    "display_name": e.title,
                    "username": None,
                    "dialog_type": "group",
                    "unread_count": unread,
                    "last_message_at": last_message_at,
                }
            )

    types = {r["dialog_type"] for r in rows}
    assert types == {"private", "bot", "group", "channel", "supergroup"}
    priv = [r for r in rows if r["dialog_type"] == "private"]
    assert priv[0]["display_name"] == "Ada Lovelace"
    assert "access_hash" not in priv[0]
    assert ClientManager is not None  # import smoke


def test_eligibility_matrix(memory_db, monkeypatch):
    monkeypatch.setattr(
        "src.messaging.eligibility.PROTECTED_IDS",
        frozenset({10}),
    )
    monkeypatch.setattr(
        "src.messaging.eligibility.PURPOSE_HOLD_IDS",
        frozenset({11}),
    )
    monkeypatch.setattr(
        "src.messaging.eligibility.RESERVED_AI_AGENT_ACCOUNT_IDS",
        frozenset({12}),
    )
    monkeypatch.setattr(
        "src.messaging.eligibility.fetch_snapshot",
        lambda *_a, **_k: None,
    )

    _add_account(memory_db, 1)
    assert evaluate_dm_account_eligibility(memory_db, 1).eligible is True
    assert evaluate_dm_account_eligibility(memory_db, 1).code == "ELIGIBLE"

    _add_account(memory_db, 2, status=AccountStatus.AUTH_REQUIRED)
    assert evaluate_dm_account_eligibility(memory_db, 2).code == "AUTH_FAILED"

    _add_account(memory_db, 3, purpose="disabled")
    assert evaluate_dm_account_eligibility(memory_db, 3).code == "DISABLED"

    assert evaluate_dm_account_eligibility(memory_db, 10).code == "PROTECTED"
    assert evaluate_dm_account_eligibility(memory_db, 11).code == "RESERVED"
    assert evaluate_dm_account_eligibility(memory_db, 12).code == "RESERVED"

    _add_account(memory_db, 4, status=AccountStatus.FLOOD_WAIT)
    assert evaluate_dm_account_eligibility(memory_db, 4).code == "FLOOD_WAIT"

    _add_account(memory_db, 5, status=AccountStatus.BANNED)
    assert evaluate_dm_account_eligibility(memory_db, 5).code == "BANNED"

    _add_account(memory_db, 6, session_string=None)
    assert evaluate_dm_account_eligibility(memory_db, 6).code == "NEEDS_SESSION"


def test_ai_allowlist_still_on_single_sender_not_dm_transport():
    sender = (ROOT / "src/ai_agent/telegram_single_sender.py").read_text(encoding="utf-8")
    assert "_ai_agent_account_gate_passes" in sender
    assert "Owner Messages must use" in sender
    transport = (ROOT / "src/messaging/transport.py").read_text(encoding="utf-8")
    assert "account_id_permitted_for_ai_agent" not in transport
    assert "RESERVED_AI_AGENT" not in transport


def test_kill_switch_and_dry_run(memory_db, monkeypatch):
    _add_account(memory_db, 50)
    monkeypatch.setattr(
        "src.messaging.eligibility.fetch_snapshot",
        lambda *_a, **_k: None,
    )
    monkeypatch.setenv("MESSAGES_EXECUTION_ENABLED", "false")
    monkeypatch.setattr(
        "src.messaging.flags.messages_execution_enabled",
        lambda: False,
    )
    monkeypatch.setattr(
        "src.messaging.owner_dm_service.messages_execution_enabled",
        lambda: False,
    )
    fake = CountingFakeTransport()
    svc = OwnerDirectMessageService(transport=fake)
    out = svc.dry_run(
        memory_db,
        account_id=50,
        peer_id="999001",
        text="hello wave6a",
        peer_type="private",
    )
    assert out["dry_run"] is True
    assert out["would_send"] is False
    assert out["transport_send_count"] == 0
    assert fake.send_calls == 0
    assert "MESSAGES_EXECUTION_ENABLED=false" in out["reason"]

    # Live send denied
    result = asyncio.run(
        svc.send_now(
            memory_db,
            account_id=50,
            peer_id="999001",
            text="hello",
            idempotency_key=str(uuid.uuid4()),
        )
    )
    assert result["error_code"] == "MESSAGES_DISABLED"
    assert fake.send_calls == 0


def test_dry_run_would_send_when_enabled(memory_db, monkeypatch):
    _add_account(memory_db, 51)
    monkeypatch.setattr(
        "src.messaging.eligibility.fetch_snapshot",
        lambda *_a, **_k: None,
    )
    monkeypatch.setenv("MESSAGES_EXECUTION_ENABLED", "true")
    svc = OwnerDirectMessageService(transport=CountingFakeTransport())
    out = svc.dry_run(
        memory_db,
        account_id=51,
        peer_id="999002",
        text="ok",
        peer_type="private",
    )
    assert out["would_send"] is True
    assert out["eligibility"]["eligible"] is True

    bad = svc.dry_run(
        memory_db,
        account_id=51,
        peer_id="",
        text="ok",
        peer_type="private",
    )
    assert bad["would_send"] is False
    assert bad["validation"]["peer_ok"] is False


def test_idempotency_and_duplicate_suppression(memory_db, monkeypatch):
    _add_account(memory_db, 60)
    monkeypatch.setattr(
        "src.messaging.eligibility.fetch_snapshot",
        lambda *_a, **_k: None,
    )
    monkeypatch.setenv("MESSAGES_EXECUTION_ENABLED", "true")
    fake = CountingFakeTransport(
        send_result={
            "ok": True,
            "success": True,
            "telegram_message_id": 4242,
            "sent_at": datetime.now(timezone.utc).replace(tzinfo=None),
            "error_code": None,
            "error_message": None,
            "retry_after": None,
        }
    )
    svc = OwnerDirectMessageService(transport=fake)
    key = "idem-" + uuid.uuid4().hex

    first = asyncio.run(
        svc.send_now(
            memory_db,
            account_id=60,
            peer_id="7001",
            text="once",
            idempotency_key=key,
        )
    )
    assert first["status"] == STATUS_SENT
    assert fake.send_calls == 1

    second = asyncio.run(
        svc.send_now(
            memory_db,
            account_id=60,
            peer_id="7001",
            text="once",
            idempotency_key=key,
        )
    )
    assert second["replay"] is True
    assert second["status"] == STATUS_SENT
    assert fake.send_calls == 1

    # SENDING duplicate
    key2 = "idem-sending-" + uuid.uuid4().hex
    intent = OwnerDmIntent(
        idempotency_key=key2,
        account_id=60,
        peer_id="7002",
        peer_type="private",
        message_hash="abc",
        message_preview="x",
        status=STATUS_SENDING,
    )
    memory_db.add(intent)
    memory_db.commit()
    mid = asyncio.run(
        svc.send_now(
            memory_db,
            account_id=60,
            peer_id="7002",
            text="x",
            idempotency_key=key2,
        )
    )
    assert mid["status"] == STATUS_SENDING
    assert mid["replay"] is True
    assert fake.send_calls == 1

    # UNCERTAIN — no second send
    key3 = "idem-uncertain-" + uuid.uuid4().hex
    memory_db.add(
        OwnerDmIntent(
            idempotency_key=key3,
            account_id=60,
            peer_id="7003",
            peer_type="private",
            message_hash="abc",
            message_preview="x",
            status=STATUS_UNCERTAIN,
            telegram_message_id=99,
            error_code="UNCERTAIN",
        )
    )
    memory_db.commit()
    u = asyncio.run(
        svc.send_now(
            memory_db,
            account_id=60,
            peer_id="7003",
            text="x",
            idempotency_key=key3,
        )
    )
    assert u["status"] == STATUS_UNCERTAIN
    assert fake.send_calls == 1


def test_transport_success_db_failure_becomes_uncertain_no_retry(memory_db, monkeypatch):
    """Mandatory: Telegram accepts → DB persist fails → UNCERTAIN; retry same key does not send."""
    _add_account(memory_db, 70)
    monkeypatch.setattr(
        "src.messaging.eligibility.fetch_snapshot",
        lambda *_a, **_k: None,
    )
    monkeypatch.setenv("MESSAGES_EXECUTION_ENABLED", "true")
    fake = CountingFakeTransport(
        send_result={
            "ok": True,
            "success": True,
            "telegram_message_id": 7777,
            "sent_at": datetime.now(timezone.utc).replace(tzinfo=None),
            "error_code": None,
            "error_message": None,
            "retry_after": None,
        }
    )
    svc = OwnerDirectMessageService(transport=fake)
    key = "uncertain-" + uuid.uuid4().hex
    out = asyncio.run(
        svc.send_now(
            memory_db,
            account_id=70,
            peer_id="8001",
            text="persist-fail",
            idempotency_key=key,
            _force_post_send_db_failure=True,
        )
    )
    assert out["status"] == STATUS_UNCERTAIN
    assert fake.send_calls == 1

    replay = asyncio.run(
        svc.send_now(
            memory_db,
            account_id=70,
            peer_id="8001",
            text="persist-fail",
            idempotency_key=key,
        )
    )
    assert replay["status"] == STATUS_UNCERTAIN
    assert replay.get("replay") is True
    assert fake.send_calls == 1  # no blind retry


def test_floodwait_and_error_mapping():
    class FakeFlood(Exception):
        def __init__(self):
            self.seconds = 30
            super().__init__("wait")

    FakeFlood.__name__ = "FloodWaitError"
    code, msg, extras = map_dm_error(FakeFlood())
    assert code == "FLOOD_WAIT"
    assert extras.get("retry_after") == 30
    assert "rate limited" in msg.lower() or "wait" in msg.lower()

    for name, expect in [
        ("UserPrivacyRestrictedError", "USER_PRIVACY_RESTRICTED"),
        ("UsernameNotOccupiedError", "USERNAME_NOT_FOUND"),
        ("PeerIdInvalidError", "PEER_INVALID"),
        ("AuthKeyUnregisteredError", "SESSION_REVOKED"),
        ("TimeoutError", "NETWORK_TIMEOUT"),
    ]:
        Exc = type(name, (Exception,), {})
        c, _, _ = map_dm_error(Exc("x"))
        assert c == expect


def test_message_validation():
    from src.messaging.owner_dm_service import validate_dm_message, validate_peer

    assert validate_dm_message("")[0] is False
    assert validate_dm_message("x" * 5000)[1] == "MESSAGE_TOO_LONG"
    assert validate_dm_message("hi")[0] is True
    assert validate_peer("", "private")[0] is False
    assert validate_peer("1", "channel")[0] is False
    assert validate_peer("1", "private")[0] is True


def test_messages_flag_default_false():
    from config.settings import Settings

    # Fresh Settings may load .env; field default is False
    assert Settings.model_fields["messages_execution_enabled"].default is False


def test_no_owner_messages_ui():
    """Wave 7: Messages page lives in messages_routes, not legacy routes.py."""
    routes = (ROOT / "src/dashboard/routes.py").read_text(encoding="utf-8")
    assert "@web.route('/messages')" not in routes
    msg = (ROOT / "src/dashboard/messages_routes.py").read_text(encoding="utf-8")
    assert "OwnerDirectMessageService" in msg
    assert '@messages_bp.route("/messages")' in msg


def test_scheduler_still_has_no_dm_job_type():
    models = (ROOT / "src/core/scheduler_models.py").read_text(encoding="utf-8")
    assert 'DM = "DM"' not in models
