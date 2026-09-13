"""Wave R — chat catalog + multi-account join orchestration (no Telegram)."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.database import Base
from src.core.models import Account, AccountStatus
from src.core.scheduler_models import ChatTarget, JobStatus, MessageType, ScheduledJob
from src.messaging.chat_catalog import build_owner_chat_catalog
from src.messaging.multi_account_join import (
    JOIN_BATCH_CAP,
    MultiAccountJoinOrchestrator,
    map_join_owner_status,
)
from src.messaging.multi_account_schedule import map_readiness_status
from src.messaging.peer_ids import canonical_peer_key

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def memory_db():
    import src.core.models  # noqa: F401
    import src.core.scheduler_models  # noqa: F401
    import src.messaging.models  # noqa: F401

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


def _elig(ok=True, code="OK"):
    return SimpleNamespace(
        eligible=ok,
        code=code,
        reason=code,
        to_dict=lambda: {"eligible": ok, "code": code},
    )


def test_waveR_owners_and_ui_surface():
    assert (ROOT / "src/messaging/chat_catalog.py").is_file()
    assert (ROOT / "src/messaging/multi_account_join.py").is_file()
    routes = (ROOT / "src/dashboard/messages_routes.py").read_text(encoding="utf-8")
    assert "chat-catalog" in routes
    assert "join-bulk" in routes
    assert "MultiAccountJoinOrchestrator" in routes
    assert "OwnerChatService" in (ROOT / "src/messaging/multi_account_join.py").read_text()
    assert "join_async" in (ROOT / "src/messaging/multi_account_join.py").read_text()
    assert "send_now" not in (ROOT / "src/messaging/multi_account_join.py").read_text()
    tpl = (ROOT / "src/dashboard/templates/messages.html").read_text(encoding="utf-8")
    assert "Request join" in tpl
    assert "Refresh status" in tpl
    assert "Select all not joined" in tpl
    assert "catalog-recent" in tpl
    assert "AUTO_SEND" not in tpl or "No messages were sent" in tpl


def test_canonical_peer_key_dedupe():
    assert canonical_peer_key("4297144441", "supergroup") == canonical_peer_key(
        "-1004297144441", "supergroup"
    )
    assert canonical_peer_key("4297144441", "channel") == "channel:4297144441"
    assert canonical_peer_key("@Armcrypto", None) == canonical_peer_key("@armcrypto", "channel")


def test_chat_catalog_dedupe_and_names(memory_db):
    memory_db.add(
        ChatTarget(
            tg_id=4452641630,
            title="Storyfleet WaveQ Canary",
            chat_type="supergroup",
            invite_link="https://t.me/+tuRPWMOBSpQwNTZh",
        )
    )
    memory_db.add(
        ScheduledJob(
            account_id=106,
            type=MessageType.DM.value,
            run_at=datetime.utcnow(),
            status=JobStatus.SENT.value,
            peer_id="-1004452641630",
            peer_type="supergroup",
            message_body="hi",
            schedule_timezone="Asia/Yerevan",
        )
    )
    # Duplicate bare id form
    memory_db.add(
        ScheduledJob(
            account_id=107,
            type=MessageType.DM.value,
            run_at=datetime.utcnow() - timedelta(hours=1),
            status=JobStatus.SENT.value,
            peer_id="4452641630",
            peer_type="supergroup",
            message_body="hi2",
            schedule_timezone="Asia/Yerevan",
        )
    )
    memory_db.commit()
    cat = build_owner_chat_catalog(memory_db, limit=20)
    assert cat["ok"] is True
    keys = [r["key"] for r in cat["known"]]
    assert len(keys) == len(set(keys))
    titles = [r["title"] for r in cat["known"]]
    assert "Storyfleet WaveQ Canary" in titles
    assert not any(str(t).lstrip("-").isdigit() for t in titles if t)


def test_chat_catalog_search(memory_db):
    memory_db.add(
        ChatTarget(tg_id=1, title="Storyfleet Test Group", chat_type="supergroup", username=None)
    )
    memory_db.add(
        ChatTarget(tg_id=2, title="Armcrypto", chat_type="channel", username="armcrypto")
    )
    memory_db.commit()
    cat = build_owner_chat_catalog(memory_db, q="storyfleet")
    assert len(cat["known"]) == 1
    assert "Storyfleet" in cat["known"][0]["title"]


def test_waiting_approval_and_ready_mapping():
    w = map_readiness_status(
        eligible=True,
        eligibility_code="OK",
        preview={"ok": True, "already_joined": False, "message": "Waiting for admin approval"},
    )
    assert w[0] == "waiting_approval" and w[2] is False
    r = map_readiness_status(
        eligible=True,
        eligibility_code="OK",
        preview={"ok": True, "already_joined": True, "can_post": True},
    )
    assert r == ("ready", "Ready", True)
    # Approval alone does not schedule — Ready only enables selection
    assert w[2] is False


def test_join_status_mapping():
    assert map_join_owner_status({"ok": True, "status": "joined"})[1] == "Joined"
    assert map_join_owner_status({"ok": True, "status": "join_requested"})[1] == "Waiting for approval"
    assert map_join_owner_status({"ok": True, "status": "already_joined"})[1] == "Already joined"
    assert map_join_owner_status({"ok": False, "error": "FLOOD_WAIT"})[0] == "failed"


def test_bulk_join_partial_and_protected(memory_db):
    for aid, name in [(106, "Rachael"), (107, "Krystal"), (108, "ProtectedOne")]:
        memory_db.add(
            Account(
                id=aid,
                phone_number=f"+1{aid}",
                status=AccountStatus.ACTIVE,
                purpose="messaging",
                session_string="s",
                first_name=name,
            )
        )
    memory_db.commit()

    outcomes = {
        106: {"ok": True, "status": "joined", "message": "Joined"},
        107: {"ok": True, "status": "join_requested", "message": "Waiting for admin approval"},
    }

    class FakeChat:
        async def join_async(self, account_id, ref, confirm=False):
            return outcomes[int(account_id)]

    def _run(coro):
        import asyncio

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)

    def elig(db, aid):
        if int(aid) == 108:
            return _elig(False, "PROTECTED")
        return _elig(True)

    orch = MultiAccountJoinOrchestrator(chat_service=FakeChat(), run_async=_run, pause_sec=0)
    with patch(
        "src.messaging.multi_account_join.evaluate_dm_account_eligibility",
        side_effect=elig,
    ):
        r = orch.join_bulk(
            memory_db,
            ref="https://t.me/+test",
            account_ids=[106, 107, 108],
            confirm=True,
        )
    assert r.ok
    assert r.payload["joined"] == 1
    assert r.payload["waiting_approval"] == 1
    assert r.payload["skipped"] == 1
    assert r.payload["auto_send"] is False
    assert r.payload["auto_schedule"] is False
    assert r.payload["scheduled_count"] == 0
    assert JOIN_BATCH_CAP == 10


def test_bulk_join_requires_confirm(memory_db):
    orch = MultiAccountJoinOrchestrator(pause_sec=0)
    r = orch.join_bulk(memory_db, ref="x", account_ids=[106], confirm=False)
    assert r.status_code == 400
    assert r.payload["error"] == "CONFIRM_REQUIRED"


def test_flood_stops_remaining(memory_db):
    for aid in (106, 107, 108):
        memory_db.add(
            Account(
                id=aid,
                phone_number=f"+1{aid}",
                status=AccountStatus.ACTIVE,
                purpose="messaging",
                session_string="s",
                first_name=f"A{aid}",
            )
        )
    memory_db.commit()
    calls = []

    class FakeChat:
        async def join_async(self, account_id, ref, confirm=False):
            calls.append(int(account_id))
            if int(account_id) == 106:
                return {"ok": False, "status": "failed", "error": "FLOOD_WAIT", "message": "wait"}
            return {"ok": True, "status": "joined"}

    def _run(coro):
        import asyncio

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)

    orch = MultiAccountJoinOrchestrator(chat_service=FakeChat(), run_async=_run, pause_sec=0)
    with patch(
        "src.messaging.multi_account_join.evaluate_dm_account_eligibility",
        return_value=_elig(),
    ):
        r = orch.join_bulk(
            memory_db, ref="x", account_ids=[106, 107, 108], confirm=True
        )
    assert calls == [106]
    assert r.payload["skipped"] == 2
