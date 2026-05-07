"""
SQLite lock retry behavior for pacing defer (scheduled_jobs updates).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.core.models  # noqa: F401
import src.core.scheduler_models  # noqa: F401
from src.core.database import Base, run_with_sqlite_lock_retry
from src.core.models import Account, AccountStatus
from src.core.scheduler_models import ChatTarget, JobStatus, MessageType, ScheduledJob
from src.scheduler.pacing import defer_scheduled_job_for_pacing


@pytest.fixture
def patched_db_session(monkeypatch):
    """Route ORM sessions to an isolated in-memory SQLite DB."""
    from src.core import database as database_module

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(database_module, "engine", engine)
    monkeypatch.setattr(database_module, "SessionLocal", TestSession)
    return TestSession


def test_defer_scheduled_job_for_pacing_retries_sqlite_lock(patched_db_session, monkeypatch):
    SessionFactory = patched_db_session
    db = SessionFactory()
    now = datetime(2026, 5, 1, 12, 0, 0)
    next_at = now + timedelta(hours=1)

    acc = Account(
        phone_number="+10000000001",
        status=AccountStatus.ACTIVE,
    )
    tgt = ChatTarget(chat_type="group", title="t")
    db.add(acc)
    db.add(tgt)
    db.flush()
    job = ScheduledJob(
        account_id=acc.id,
        target_id=tgt.id,
        type=MessageType.PROMO.value,
        run_at=now,
        status=JobStatus.PENDING.value,
    )
    db.add(job)
    real_commit = Session.commit
    real_commit(db)
    job_id = int(job.id)
    db.close()

    commits = {"n": 0}

    def flaky_commit(self):
        commits["n"] += 1
        if commits["n"] == 1:
            raise OperationalError(
                "COMMIT",
                {},
                sqlite3.OperationalError("database is locked"),
            )
        return real_commit(self)

    monkeypatch.setattr(Session, "commit", flaky_commit)
    monkeypatch.setattr("src.core.database._is_sqlite", lambda _url: True)

    defer_scheduled_job_for_pacing(None, job_id, next_at)

    assert commits["n"] == 2

    verify = SessionFactory()
    row = verify.query(ScheduledJob).filter(ScheduledJob.id == job_id).one()
    assert row.run_at == next_at
    assert row.last_error == "pacing_deferred"
    verify.close()

    # Single logical row update — still one scheduled job
    c2 = SessionFactory()
    assert c2.query(ScheduledJob).filter(ScheduledJob.id == job_id).count() == 1
    c2.close()


def test_run_with_sqlite_lock_retry_non_lock_error_not_retried(monkeypatch):
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise OperationalError("x", {}, sqlite3.OperationalError("no such table: nope"))

    monkeypatch.setattr("src.core.database._is_sqlite", lambda _url: True)

    with pytest.raises(OperationalError):
        run_with_sqlite_lock_retry(boom, operation="test_op", job_id=1)

    assert calls["n"] == 1
