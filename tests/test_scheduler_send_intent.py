"""Durable send-intent: SENDING / UNCERTAIN / finalize same MessageDelivery row."""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.core.models  # noqa: F401
import src.core.scheduler_models  # noqa: F401
from src.core.database import Base
from src.core.models import Account, AccountStatus
from src.core.scheduler_models import (
    ChatTarget,
    DeliveryStatus,
    JobStatus,
    MessageDelivery,
    MessageType,
    ScheduledJob,
)
from src.scheduler import executor as executor_mod


@pytest.fixture
def si_db(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from src.core import database as database_module

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(database_module, "engine", engine)
    monkeypatch.setattr(database_module, "SessionLocal", TestSession)
    return TestSession


def _seed_runnable_job(SessionFactory):
    db = SessionFactory()
    acc = Account(phone_number="+19995550222", status=AccountStatus.ACTIVE)
    tgt = ChatTarget(chat_type="group", title="g", username="validchannel", tg_id=9001)
    db.add(acc)
    db.add(tgt)
    db.flush()
    from src.core.scheduler_models import AccountTargetBinding

    b = AccountTargetBinding(account_id=acc.id, target_id=tgt.id, can_post=True)
    db.add(b)
    db.flush()
    job = ScheduledJob(
        account_id=acc.id,
        target_id=tgt.id,
        type=MessageType.PROMO.value,
        run_at=datetime(2026, 5, 8, 12, 0, 0),
        status=JobStatus.RUNNING.value,
        lease_until=datetime(2099, 12, 31, 23, 59, 59),
        lease_owner="w1",
    )
    db.add(job)
    db.commit()
    jid, aid, tid = int(job.id), int(acc.id), int(tgt.id)
    db.close()
    return jid, aid, tid


def _pace_allow(*_a, **_k):
    return {
        "allowed": True,
        "retry_after_sec": 0,
        "reasons": [],
        "next_allowed_at": None,
        "next_allowed_at_dt": None,
    }


@pytest.mark.asyncio
async def test_sending_row_exists_before_send_message_invoked(si_db, monkeypatch):
    SessionFactory = si_db
    jid, *_ = _seed_runnable_job(SessionFactory)
    monkeypatch.setattr("src.scheduler.pacing.get_send_pacing_decision", _pace_allow)
    monkeypatch.setattr(executor_mod, "_get_template_body", lambda *a, **k: "x {account_name}")
    monkeypatch.setattr(executor_mod, "_resolve_send_entity", AsyncMock(return_value=object()))

    observed = []

    async def fake_execute(_fn, _ent, _body):
        s = SessionFactory()
        row = (
            s.query(MessageDelivery)
            .filter(MessageDelivery.job_id == jid)
            .order_by(MessageDelivery.id.desc())
            .first()
        )
        observed.append(str(row.status).upper() if row else None)
        s.close()
        m = MagicMock()
        m.id = 424242
        return m

    w = MagicMock()
    w.is_connected = True
    w.client = MagicMock()
    w.execute = AsyncMock(side_effect=fake_execute)

    monkeypatch.setattr(executor_mod.client_manager, "add_account", AsyncMock(return_value=(w, None)))
    monkeypatch.setattr(executor_mod.client_manager, "remove_account", AsyncMock())

    ok = await executor_mod.execute_job(jid)
    assert ok is True
    assert observed == ["SENDING"]
    v = SessionFactory()
    rows = v.query(MessageDelivery).filter(MessageDelivery.job_id == jid).all()
    assert len(rows) == 1
    assert rows[0].status == DeliveryStatus.SENT.value
    assert int(rows[0].tg_message_id or 0) == 424242
    v.close()


@pytest.mark.asyncio
async def test_success_updates_same_row_to_sent(si_db, monkeypatch):
    SessionFactory = si_db
    jid, *_ = _seed_runnable_job(SessionFactory)
    monkeypatch.setattr("src.scheduler.pacing.get_send_pacing_decision", _pace_allow)
    monkeypatch.setattr(executor_mod, "_get_template_body", lambda *a, **k: "hi")
    monkeypatch.setattr(executor_mod, "_resolve_send_entity", AsyncMock(return_value=object()))

    async def fake_execute(_fn, _ent, _body):
        m = MagicMock()
        m.id = 1001
        return m

    w = MagicMock()
    w.is_connected = True
    w.execute = AsyncMock(side_effect=fake_execute)
    monkeypatch.setattr(executor_mod.client_manager, "add_account", AsyncMock(return_value=(w, None)))
    monkeypatch.setattr(executor_mod.client_manager, "remove_account", AsyncMock())

    await executor_mod.execute_job(jid)
    v = SessionFactory()
    assert v.query(MessageDelivery).filter(MessageDelivery.job_id == jid).count() == 1
    v.close()


@pytest.mark.asyncio
async def test_timeout_marks_uncertain(si_db, monkeypatch):
    SessionFactory = si_db
    jid, *_ = _seed_runnable_job(SessionFactory)
    monkeypatch.setattr("src.scheduler.pacing.get_send_pacing_decision", _pace_allow)
    monkeypatch.setattr(executor_mod, "_get_template_body", lambda *a, **k: "hi")
    monkeypatch.setattr(executor_mod, "_resolve_send_entity", AsyncMock(return_value=object()))

    w = MagicMock()
    w.is_connected = True
    w.execute = AsyncMock(return_value=MagicMock(id=1))
    monkeypatch.setattr(executor_mod.client_manager, "add_account", AsyncMock(return_value=(w, None)))
    monkeypatch.setattr(executor_mod.client_manager, "remove_account", AsyncMock())

    async def boom_wait_for(_coro, timeout=None):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(executor_mod.asyncio, "wait_for", boom_wait_for)

    ok = await executor_mod.execute_job(jid)
    assert ok is False
    v = SessionFactory()
    row = v.query(MessageDelivery).filter(MessageDelivery.job_id == jid).one()
    assert row.status == DeliveryStatus.UNCERTAIN.value
    job = v.query(ScheduledJob).filter(ScheduledJob.id == jid).one()
    assert job.status == JobStatus.RUNNING.value
    v.close()


@pytest.mark.asyncio
async def test_known_telethon_failure_marks_failed(si_db, monkeypatch):
    SessionFactory = si_db
    jid, *_ = _seed_runnable_job(SessionFactory)
    monkeypatch.setattr("src.scheduler.pacing.get_send_pacing_decision", _pace_allow)
    monkeypatch.setattr(executor_mod, "_get_template_body", lambda *a, **k: "hi")
    monkeypatch.setattr(executor_mod, "_resolve_send_entity", AsyncMock(return_value=object()))

    w = MagicMock()
    w.is_connected = True
    w.execute = AsyncMock(side_effect=executor_mod.UserRestrictedError("nope"))
    monkeypatch.setattr(executor_mod.client_manager, "add_account", AsyncMock(return_value=(w, None)))
    monkeypatch.setattr(executor_mod.client_manager, "remove_account", AsyncMock())

    ok = await executor_mod.execute_job(jid)
    assert ok is False
    v = SessionFactory()
    row = v.query(MessageDelivery).filter(MessageDelivery.job_id == jid).one()
    assert row.status == DeliveryStatus.FAILED.value
    job = v.query(ScheduledJob).filter(ScheduledJob.id == jid).one()
    assert job.status == JobStatus.FAILED.value
    assert job.lease_until is None
    v.close()


@pytest.mark.asyncio
async def test_active_sending_and_lease_blocks_second_send(si_db, monkeypatch):
    SessionFactory = si_db
    jid, aid, tid = _seed_runnable_job(SessionFactory)
    db = SessionFactory()
    db.add(
        MessageDelivery(
            job_id=jid,
            account_id=aid,
            target_id=tid,
            type=MessageType.PROMO.value,
            status=DeliveryStatus.SENDING.value,
        )
    )
    db.commit()
    db.close()

    monkeypatch.setattr("src.scheduler.pacing.get_send_pacing_decision", _pace_allow)
    monkeypatch.setattr(executor_mod, "_get_template_body", lambda *a, **k: "hi")
    monkeypatch.setattr(executor_mod, "_resolve_send_entity", AsyncMock(return_value=object()))

    mock_add = AsyncMock()
    monkeypatch.setattr(executor_mod.client_manager, "add_account", mock_add)

    ok = await executor_mod.execute_job(jid)
    assert ok is False
    mock_add.assert_not_called()


@pytest.mark.asyncio
async def test_uncertain_row_blocks_and_fails_job(si_db, monkeypatch):
    SessionFactory = si_db
    jid, aid, tid = _seed_runnable_job(SessionFactory)
    db = SessionFactory()
    db.add(
        MessageDelivery(
            job_id=jid,
            account_id=aid,
            target_id=tid,
            type=MessageType.PROMO.value,
            status=DeliveryStatus.UNCERTAIN.value,
            error_code="X",
            error_message="prior",
        )
    )
    db.commit()
    db.close()

    monkeypatch.setattr("src.scheduler.pacing.get_send_pacing_decision", _pace_allow)
    monkeypatch.setattr(executor_mod, "_get_template_body", lambda *a, **k: "hi")
    monkeypatch.setattr(executor_mod, "_resolve_send_entity", AsyncMock(return_value=object()))

    mock_add = AsyncMock()
    monkeypatch.setattr(executor_mod.client_manager, "add_account", mock_add)

    ok = await executor_mod.execute_job(jid)
    assert ok is False
    mock_add.assert_not_called()
    v = SessionFactory()
    job = v.query(ScheduledJob).filter(ScheduledJob.id == jid).one()
    assert job.status == JobStatus.FAILED.value
    assert job.lease_until is None
    v.close()


def test_reconcile_sent_still_works(si_db):
    SessionFactory = si_db
    jid, aid, tid = _seed_runnable_job(SessionFactory)
    db = SessionFactory()
    db.add(
        MessageDelivery(
            job_id=jid,
            account_id=aid,
            target_id=tid,
            type=MessageType.PROMO.value,
            status="SENT",
            sent_at=datetime.utcnow(),
            tg_message_id=111,
        )
    )
    db.commit()
    db.close()
    assert executor_mod._reconcile_job_if_sent_delivery_exists(jid) is True
    v = SessionFactory()
    job = v.query(ScheduledJob).filter(ScheduledJob.id == jid).one()
    assert job.status == JobStatus.SENT.value
    v.close()


@pytest.mark.asyncio
async def test_stale_sending_cleared_then_new_attempt_succeeds(si_db, monkeypatch):
    SessionFactory = si_db
    jid, aid, tid = _seed_runnable_job(SessionFactory)
    db = SessionFactory()
    job = db.query(ScheduledJob).filter(ScheduledJob.id == jid).one()
    job.lease_until = datetime(2026, 5, 8, 10, 0, 0)
    db.add(
        MessageDelivery(
            job_id=jid,
            account_id=aid,
            target_id=tid,
            type=MessageType.PROMO.value,
            status=DeliveryStatus.SENDING.value,
        )
    )
    db.commit()
    db.close()

    monkeypatch.setattr("src.scheduler.pacing.get_send_pacing_decision", _pace_allow)
    monkeypatch.setattr(executor_mod, "_get_template_body", lambda *a, **k: "hi")
    monkeypatch.setattr(executor_mod, "_resolve_send_entity", AsyncMock(return_value=object()))

    async def fake_execute(_fn, _ent, _body):
        m = MagicMock()
        m.id = 777
        return m

    w = MagicMock()
    w.is_connected = True
    w.execute = AsyncMock(side_effect=fake_execute)
    monkeypatch.setattr(executor_mod.client_manager, "add_account", AsyncMock(return_value=(w, None)))
    monkeypatch.setattr(executor_mod.client_manager, "remove_account", AsyncMock())

    ok = await executor_mod.execute_job(jid)
    assert ok is True
    v = SessionFactory()
    rows = v.query(MessageDelivery).filter(MessageDelivery.job_id == jid).order_by(MessageDelivery.id.asc()).all()
    assert len(rows) == 2
    assert rows[0].status == DeliveryStatus.FAILED.value
    assert rows[0].error_code == "STALE_SENDING"
    assert rows[1].status == DeliveryStatus.SENT.value
    v.close()