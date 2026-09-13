"""Final P1 hardening — bulk idempotency, service chats, exec-time recheck."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.database import Base
from src.core.models import Account, AccountStatus
from src.core.scheduler_models import JobStatus, MessageType, ScheduledJob
from src.messaging.multi_account_schedule import MultiAccountScheduleOrchestrator
from src.messaging.telegram_service_peers import (
    is_sensitive_telegram_system_chat,
    mask_sensitive_message_text,
)
from src.scheduler.executor import _execute_scheduled_dm_job


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


def _add_account(db, aid: int, name: str = "Acc"):
    a = Account(
        id=aid,
        phone_number=f"+1555{aid:07d}",
        status=AccountStatus.ACTIVE,
        purpose="messaging",
        session_string="s",
        first_name=name,
    )
    db.add(a)
    db.commit()
    return a


def _elig():
    return SimpleNamespace(eligible=True, code="OK", reason="ok", to_dict=lambda: {})


def _run(coro):
    import asyncio

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


def _orch(db_accounts=(106, 107)):
    class FakeChat:
        async def preview_async(self, account_id, ref):
            return {
                "ok": True,
                "already_joined": True,
                "can_post": True,
                "title": "Test Group",
                "chat_type": "supergroup",
                "peer_id": "-1004297144441",
            }

    o = MultiAccountScheduleOrchestrator(chat_service=FakeChat())
    o._run_async = _run
    return o


def _create_kwargs(future, *, key, message="hello bulk", accounts=None):
    return dict(
        ref="https://t.me/+test",
        account_ids=accounts or [106, 107],
        message=message,
        local_date=future,
        local_time="10:00:00",
        timezone_name="Asia/Yerevan",
        spacing_sec=60,
        peer_type="supergroup",
        idempotency_key=key,
    )


@pytest.fixture
def future():
    return (datetime.utcnow() + timedelta(days=3)).strftime("%Y-%m-%d")


def test_missing_bulk_key_rejected(memory_db, future):
    for aid in (106, 107):
        _add_account(memory_db, aid)
    orch = _orch()
    with patch("src.messaging.scheduled_dm_flags.scheduled_dm_enabled", return_value=True), patch(
        "src.messaging.chat_flags.messages_group_channel_send_enabled", return_value=True
    ), patch(
        "src.messaging.multi_account_schedule.evaluate_dm_account_eligibility",
        return_value=_elig(),
    ), patch(
        "src.messaging.scheduled_dm_service.evaluate_dm_account_eligibility",
        return_value=_elig(),
    ):
        r = orch.create_bulk(
            memory_db,
            ref="https://t.me/+test",
            account_ids=[106, 107],
            message="x",
            local_date=future,
            local_time="10:00:00",
            timezone_name="Asia/Yerevan",
            spacing_sec=60,
            peer_type="supergroup",
            idempotency_key=None,
        )
    assert r.status_code == 422
    assert r.payload["error_code"] == "BULK_IDEMPOTENCY_KEY_REQUIRED"
    assert memory_db.query(ScheduledJob).count() == 0


def test_same_key_same_payload_replay(memory_db, future):
    for aid in (106, 107):
        _add_account(memory_db, aid)
    orch = _orch()
    kw = _create_kwargs(future, key="bulk-same-1")
    with patch("src.messaging.scheduled_dm_flags.scheduled_dm_enabled", return_value=True), patch(
        "src.messaging.chat_flags.messages_group_channel_send_enabled", return_value=True
    ), patch(
        "src.messaging.multi_account_schedule.evaluate_dm_account_eligibility",
        return_value=_elig(),
    ), patch(
        "src.messaging.scheduled_dm_service.evaluate_dm_account_eligibility",
        return_value=_elig(),
    ):
        r1 = orch.create_bulk(memory_db, **kw)
        r2 = orch.create_bulk(memory_db, **kw)
    assert r1.ok and r2.ok
    assert r2.payload.get("replay") is True
    assert memory_db.query(ScheduledJob).filter(ScheduledJob.type == "DM").count() == 2


def test_same_key_different_payload_conflict(memory_db, future):
    for aid in (106, 107):
        _add_account(memory_db, aid)
    orch = _orch()
    with patch("src.messaging.scheduled_dm_flags.scheduled_dm_enabled", return_value=True), patch(
        "src.messaging.chat_flags.messages_group_channel_send_enabled", return_value=True
    ), patch(
        "src.messaging.multi_account_schedule.evaluate_dm_account_eligibility",
        return_value=_elig(),
    ), patch(
        "src.messaging.scheduled_dm_service.evaluate_dm_account_eligibility",
        return_value=_elig(),
    ):
        r1 = orch.create_bulk(memory_db, **_create_kwargs(future, key="bulk-conflict", message="A"))
        r2 = orch.create_bulk(memory_db, **_create_kwargs(future, key="bulk-conflict", message="B"))
    assert r1.ok
    assert r2.status_code == 409
    assert r2.payload["error_code"] == "IDEMPOTENCY_KEY_CONFLICT"
    assert memory_db.query(ScheduledJob).filter(ScheduledJob.type == "DM").count() == 2


def test_two_tab_replay_no_duplicates(memory_db, future):
    """Simulate lost-response + second tab using the same key."""
    for aid in (106, 107):
        _add_account(memory_db, aid)
    orch = _orch()
    kw = _create_kwargs(future, key="bulk-twotab")
    with patch("src.messaging.scheduled_dm_flags.scheduled_dm_enabled", return_value=True), patch(
        "src.messaging.chat_flags.messages_group_channel_send_enabled", return_value=True
    ), patch(
        "src.messaging.multi_account_schedule.evaluate_dm_account_eligibility",
        return_value=_elig(),
    ), patch(
        "src.messaging.scheduled_dm_service.evaluate_dm_account_eligibility",
        return_value=_elig(),
    ):
        a = orch.create_bulk(memory_db, **kw)
        b = orch.create_bulk(memory_db, **kw)
        c = orch.create_bulk(memory_db, **kw)
    assert a.payload["created"] == 2
    assert b.payload.get("replay") is True
    assert c.payload.get("replay") is True
    assert memory_db.query(ScheduledJob).filter(ScheduledJob.type == "DM").count() == 2


def test_partial_create_replay_keeps_successful(memory_db, future):
    for aid in (106, 107, 108):
        _add_account(memory_db, aid, name=f"A{aid}")

    real_schedule = None

    class FakeChat:
        async def preview_async(self, account_id, ref):
            return {
                "ok": True,
                "already_joined": True,
                "can_post": True,
                "title": "G",
                "chat_type": "supergroup",
                "peer_id": "-1001",
            }

    orch = MultiAccountScheduleOrchestrator(chat_service=FakeChat())
    orch._run_async = _run
    calls = {"n": 0}
    original = orch._sched.schedule

    def flaky_schedule(db, **kwargs):
        calls["n"] += 1
        # Fail account 108 on first wave only
        if int(kwargs.get("account_id") or 0) == 108 and calls["n"] <= 3:
            from src.messaging.scheduled_dm_service import ScheduleDmResult

            return ScheduleDmResult(
                False,
                429,
                {"ok": False, "error": "RATE_LIMITED", "message": "rate"},
            )
        return original(db, **kwargs)

    orch._sched.schedule = flaky_schedule  # type: ignore
    kw = _create_kwargs(future, key="bulk-partial", accounts=[106, 107, 108])
    with patch("src.messaging.scheduled_dm_flags.scheduled_dm_enabled", return_value=True), patch(
        "src.messaging.chat_flags.messages_group_channel_send_enabled", return_value=True
    ), patch(
        "src.messaging.multi_account_schedule.evaluate_dm_account_eligibility",
        return_value=_elig(),
    ), patch(
        "src.messaging.scheduled_dm_service.evaluate_dm_account_eligibility",
        return_value=_elig(),
    ):
        r1 = orch.create_bulk(memory_db, **kw)
        count1 = memory_db.query(ScheduledJob).filter(ScheduledJob.type == "DM").count()
        r2 = orch.create_bulk(memory_db, **kw)
        count2 = memory_db.query(ScheduledJob).filter(ScheduledJob.type == "DM").count()
    assert r1.payload["created"] == 2
    assert r1.payload["failed"] == 1
    assert count2 == count1  # replay must not add jobs
    assert r2.payload.get("replay") is True


def test_service_peer_classification_and_mask():
    assert is_sensitive_telegram_system_chat(777000) is True
    assert is_sensitive_telegram_system_chat("777000") is True
    assert is_sensitive_telegram_system_chat("-1004297144441", peer_type="supergroup") is False
    masked = mask_sensitive_message_text("Login code: 12345")
    assert "12345" not in masked
    assert "Sensitive code hidden" in masked


def test_ai_draft_blocks_service_chat(memory_db):
    import asyncio
    from src.messaging.message_draft_service import MessageDraftService

    _add_account(memory_db, 106)
    called = {"n": 0}

    class FakeProvider:
        def generate(self, req):
            called["n"] += 1
            return SimpleNamespace(ok=True, text="hi", error_code=None, error_message=None)

    svc = MessageDraftService(provider=FakeProvider())
    with patch("src.messaging.message_draft_service.messages_ai_draft_enabled", return_value=True), patch(
        "src.messaging.message_draft_service.evaluate_dm_account_eligibility",
        return_value=_elig(),
    ):
        out = asyncio.run(
            svc.draft_reply_async(memory_db, account_id=106, peer="777000")
        )
    assert out["ok"] is False
    assert out["error_code"] == "SENSITIVE_TELEGRAM_CHAT"
    assert called["n"] == 0


def test_send_now_blocks_service_chat(memory_db):
    import asyncio
    from src.messaging.owner_dm_service import OwnerDirectMessageService

    _add_account(memory_db, 106)
    svc = OwnerDirectMessageService(transport=AsyncMock())
    with patch("src.messaging.owner_dm_service.messages_execution_enabled", return_value=True):
        r = asyncio.run(
            svc.send_now(
                memory_db,
                account_id=106,
                peer_id="777000",
                text="hi",
                idempotency_key="k-svc-1",
                peer_type="private",
            )
        )
    assert r["ok"] is False
    assert r["error_code"] == "SENSITIVE_TELEGRAM_CHAT"


def test_group_recheck_ready_allows_send(memory_db):
    _add_account(memory_db, 106)
    now = datetime.utcnow()
    job = ScheduledJob(
        account_id=106,
        type=MessageType.DM.value,
        status=JobStatus.RUNNING.value,
        run_at=now - timedelta(minutes=1),
        peer_id="-1004297144441",
        peer_type="supergroup",
        message_body="ping",
        created_at=now,
        updated_at=now,
        lease_owner="w1",
    )
    memory_db.add(job)
    memory_db.commit()
    jid = job.id

    @contextmanager
    def _ctx():
        try:
            yield memory_db
            memory_db.commit()
        except Exception:
            memory_db.rollback()
            raise

    send = AsyncMock(
        return_value={"ok": True, "status": "SENT", "telegram_message_id": 1, "replay": False}
    )
    preview_mock = AsyncMock(
        return_value={
            "ok": True,
            "already_joined": True,
            "can_post": True,
            "title": "G",
            "chat_type": "supergroup",
            "peer_id": "-1004297144441",
        }
    )
    with patch("src.scheduler.executor.get_db_context", _ctx), patch(
        "src.messaging.eligibility.evaluate_dm_account_eligibility", return_value=_elig()
    ), patch(
        "src.messaging.owner_chat_service.OwnerChatService.preview_async", preview_mock
    ), patch(
        "src.messaging.owner_dm_service.OwnerDirectMessageService.send_now", send
    ):
        ok = _run(_execute_scheduled_dm_job(jid))
    assert ok is True
    send.assert_awaited()
    preview_mock.assert_awaited()


def test_group_recheck_removed_fails_without_send(memory_db):
    _add_account(memory_db, 106)
    now = datetime.utcnow()
    job = ScheduledJob(
        account_id=106,
        type=MessageType.DM.value,
        status=JobStatus.RUNNING.value,
        run_at=now - timedelta(minutes=1),
        peer_id="-1004297144441",
        peer_type="supergroup",
        message_body="ping",
        created_at=now,
        updated_at=now,
        lease_owner="w1",
    )
    memory_db.add(job)
    memory_db.commit()
    jid = job.id

    @contextmanager
    def _ctx():
        try:
            yield memory_db
            memory_db.commit()
        except Exception:
            memory_db.rollback()
            raise

    preview_mock = AsyncMock(
        return_value={
            "ok": True,
            "already_joined": False,
            "can_post": None,
            "message": "Not a member",
            "chat_type": "supergroup",
            "peer_id": "-1004297144441",
        }
    )
    send = AsyncMock()
    with patch("src.scheduler.executor.get_db_context", _ctx), patch(
        "src.messaging.eligibility.evaluate_dm_account_eligibility", return_value=_elig()
    ), patch(
        "src.messaging.owner_chat_service.OwnerChatService.preview_async", preview_mock
    ), patch(
        "src.messaging.owner_dm_service.OwnerDirectMessageService.send_now", send
    ):
        ok = _run(_execute_scheduled_dm_job(jid))
    assert ok is False
    send.assert_not_called()
    memory_db.refresh(job)
    assert job.status == JobStatus.FAILED.value
    assert "PERMISSION_RECHECK" in (job.last_error or "")
    assert "No longer a member" in (job.last_error or "")


def test_group_recheck_cannot_post_fails(memory_db):
    _add_account(memory_db, 106)
    now = datetime.utcnow()
    job = ScheduledJob(
        account_id=106,
        type=MessageType.DM.value,
        status=JobStatus.RUNNING.value,
        run_at=now - timedelta(minutes=1),
        peer_id="-1004297144441",
        peer_type="supergroup",
        message_body="ping",
        created_at=now,
        updated_at=now,
        lease_owner="w1",
    )
    memory_db.add(job)
    memory_db.commit()
    jid = job.id

    @contextmanager
    def _ctx():
        try:
            yield memory_db
            memory_db.commit()
        except Exception:
            memory_db.rollback()
            raise

    preview_mock = AsyncMock(
        return_value={
            "ok": True,
            "already_joined": True,
            "can_post": False,
            "chat_type": "supergroup",
            "peer_id": "-1004297144441",
        }
    )
    send = AsyncMock()
    with patch("src.scheduler.executor.get_db_context", _ctx), patch(
        "src.messaging.eligibility.evaluate_dm_account_eligibility", return_value=_elig()
    ), patch(
        "src.messaging.owner_chat_service.OwnerChatService.preview_async", preview_mock
    ), patch(
        "src.messaging.owner_dm_service.OwnerDirectMessageService.send_now", send
    ):
        ok = _run(_execute_scheduled_dm_job(jid))
    assert ok is False
    send.assert_not_called()
    memory_db.refresh(job)
    assert "Cannot post" in (job.last_error or "")


def test_private_path_skips_membership_recheck(memory_db):
    _add_account(memory_db, 106)
    now = datetime.utcnow()
    job = ScheduledJob(
        account_id=106,
        type=MessageType.DM.value,
        status=JobStatus.RUNNING.value,
        run_at=now - timedelta(minutes=1),
        peer_id="8531893204",
        peer_type="private",
        message_body="ping",
        created_at=now,
        updated_at=now,
        lease_owner="w1",
    )
    memory_db.add(job)
    memory_db.commit()
    jid = job.id

    @contextmanager
    def _ctx():
        try:
            yield memory_db
            memory_db.commit()
        except Exception:
            memory_db.rollback()
            raise

    preview = AsyncMock()
    send = AsyncMock(
        return_value={"ok": True, "status": "SENT", "telegram_message_id": 9, "replay": False}
    )
    with patch("src.scheduler.executor.get_db_context", _ctx), patch(
        "src.messaging.owner_chat_service.OwnerChatService.preview_async", preview
    ), patch(
        "src.messaging.owner_dm_service.OwnerDirectMessageService.send_now", send
    ):
        ok = _run(_execute_scheduled_dm_job(jid))
    assert ok is True
    preview.assert_not_called()
    send.assert_awaited()
