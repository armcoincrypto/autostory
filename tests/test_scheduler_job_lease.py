"""DB lease claim for scheduled_jobs (RUNNING + lease_until / lease_owner)."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.core.models  # noqa: F401
import src.core.scheduler_models  # noqa: F401
from src.core.database import Base
from src.core.models import Account, AccountStatus
from src.core.scheduler_models import ChatTarget, JobStatus, MessageType, ScheduledJob
from src.scheduler import executor as executor_mod
from src.scheduler.job_claim import claim_due_job


@pytest.fixture
def lease_db(monkeypatch):
    from src.core import database as database_module

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(database_module, "engine", engine)
    monkeypatch.setattr(database_module, "SessionLocal", TestSession)
    return TestSession


def _seed_account_target(SessionFactory):
    db = SessionFactory()
    acc = Account(phone_number="+19995550199", status=AccountStatus.ACTIVE)
    tgt = ChatTarget(chat_type="group", title="g", username="x", tg_id=1002)
    db.add(acc)
    db.add(tgt)
    db.flush()
    aid, tid = int(acc.id), int(tgt.id)
    db.commit()
    db.close()
    return aid, tid


def test_concurrent_claim_only_one_wins_same_pending_job(lease_db):
    SessionFactory = lease_db
    aid, tid = _seed_account_target(SessionFactory)
    db0 = SessionFactory()
    job = ScheduledJob(
        account_id=aid,
        target_id=tid,
        type=MessageType.PROMO.value,
        run_at=datetime(2026, 5, 8, 10, 0, 0),
        status=JobStatus.PENDING.value,
    )
    db0.add(job)
    db0.commit()
    jid = int(job.id)
    db0.close()

    now = datetime(2026, 5, 8, 12, 0, 0)
    db1 = SessionFactory()
    db2 = SessionFactory()
    cr1 = claim_due_job(db1, worker_id="w1", lease_seconds=600, now_naive=now)
    db1.commit()
    cr2 = claim_due_job(db2, worker_id="w2", lease_seconds=600, now_naive=now)
    db2.commit()
    db1.close()
    db2.close()

    assert cr1.job_id == jid
    assert cr2.job_id is None

    v = SessionFactory()
    row = v.query(ScheduledJob).filter(ScheduledJob.id == jid).one()
    assert row.status == JobStatus.RUNNING.value
    assert row.lease_owner == "w1"
    v.close()


def test_running_valid_lease_not_claimable_no_pending(lease_db):
    SessionFactory = lease_db
    aid, tid = _seed_account_target(SessionFactory)
    db0 = SessionFactory()
    past = datetime(2026, 5, 8, 9, 0, 0)
    job = ScheduledJob(
        account_id=aid,
        target_id=tid,
        type=MessageType.PROMO.value,
        run_at=past,
        status=JobStatus.RUNNING.value,
        lease_until=datetime(2026, 5, 8, 18, 0, 0),
        lease_owner="old",
    )
    db0.add(job)
    db0.commit()
    db0.close()

    now = datetime(2026, 5, 8, 12, 0, 0)
    db1 = SessionFactory()
    cr = claim_due_job(db1, worker_id="w9", lease_seconds=600, now_naive=now)
    db1.commit()
    db1.close()
    assert cr.job_id is None


def test_running_expired_lease_reclaimed(lease_db):
    SessionFactory = lease_db
    aid, tid = _seed_account_target(SessionFactory)
    db0 = SessionFactory()
    past = datetime(2026, 5, 8, 9, 0, 0)
    job = ScheduledJob(
        account_id=aid,
        target_id=tid,
        type=MessageType.PROMO.value,
        run_at=past,
        status=JobStatus.RUNNING.value,
        lease_until=datetime(2026, 5, 8, 11, 0, 0),
        lease_owner="stale_owner",
    )
    db0.add(job)
    db0.commit()
    jid = int(job.id)
    db0.close()

    now = datetime(2026, 5, 8, 12, 0, 0)
    db1 = SessionFactory()
    cr = claim_due_job(db1, worker_id="w_new", lease_seconds=600, now_naive=now)
    db1.commit()
    db1.close()

    assert cr.job_id == jid
    assert cr.stale_reclaim is True

    v = SessionFactory()
    row = v.query(ScheduledJob).filter(ScheduledJob.id == jid).one()
    assert row.lease_owner == "w_new"
    assert row.lease_until == now + timedelta(seconds=600)
    v.close()


def test_mark_job_sent_clears_lease(lease_db):
    SessionFactory = lease_db
    aid, tid = _seed_account_target(SessionFactory)
    db0 = SessionFactory()
    job = ScheduledJob(
        account_id=aid,
        target_id=tid,
        type=MessageType.PROMO.value,
        run_at=datetime(2026, 5, 8, 10, 0, 0),
        status=JobStatus.RUNNING.value,
        lease_until=datetime(2026, 5, 8, 14, 0, 0),
        lease_owner="wx",
    )
    db0.add(job)
    db0.commit()
    jid = int(job.id)
    db0.close()

    executor_mod._mark_job_sent(jid, 555001, "hello")

    v = SessionFactory()
    row = v.query(ScheduledJob).filter(ScheduledJob.id == jid).one()
    assert row.status == JobStatus.SENT.value
    assert row.lease_until is None
    assert row.lease_owner is None
    v.close()


def test_pacing_defer_running_resets_pending_and_clears_lease(lease_db):
    SessionFactory = lease_db
    aid, tid = _seed_account_target(SessionFactory)
    db0 = SessionFactory()
    job = ScheduledJob(
        account_id=aid,
        target_id=tid,
        type=MessageType.PROMO.value,
        run_at=datetime(2026, 5, 8, 10, 0, 0),
        status=JobStatus.RUNNING.value,
        lease_until=datetime(2026, 5, 8, 14, 0, 0),
        lease_owner="wp",
    )
    db0.add(job)
    db0.commit()
    jid = int(job.id)
    db0.close()

    from src.scheduler.pacing import defer_scheduled_job_for_pacing

    next_at = datetime(2026, 5, 8, 15, 30, 0)
    defer_scheduled_job_for_pacing(None, jid, next_at)

    v = SessionFactory()
    row = v.query(ScheduledJob).filter(ScheduledJob.id == jid).one()
    assert row.status == JobStatus.PENDING.value
    assert row.run_at == next_at
    assert row.last_error == "pacing_deferred"
    assert row.lease_until is None
    assert row.lease_owner is None
    v.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal",
    [JobStatus.SENT.value, JobStatus.FAILED.value, JobStatus.SKIPPED.value],
)
async def test_execute_job_returns_false_for_terminal_status(lease_db, terminal):
    SessionFactory = lease_db
    aid, tid = _seed_account_target(SessionFactory)
    db0 = SessionFactory()
    job = ScheduledJob(
        account_id=aid,
        target_id=tid,
        type=MessageType.PROMO.value,
        run_at=datetime(2026, 5, 8, 10, 0, 0),
        status=terminal,
    )
    db0.add(job)
    db0.commit()
    jid = int(job.id)
    db0.close()

    ok = await executor_mod.execute_job(jid)
    assert ok is False
