"""Idempotency guard: skip Telegram send when a SENT delivery already exists for the job."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.core.models  # noqa: F401
import src.core.scheduler_models  # noqa: F401
from src.core.database import Base
from src.core.models import Account, AccountStatus
from src.core.scheduler_models import (
    AccountTargetBinding,
    ChatTarget,
    DeliveryStatus,
    JobStatus,
    MessageDelivery,
    MessageType,
    ScheduledJob,
)
from src.scheduler import executor as executor_mod


@pytest.fixture(autouse=True)
def _scheduler_gate_proceed(monkeypatch):
    """Unit tests mock Telethon; bypass operational-state gate for send-path assertions."""
    monkeypatch.setattr(
        "src.scheduler.runtime_preflight.classify_scheduler_runtime_gate",
        lambda _db, _account: {
            "action": "proceed",
            "primary_blocker": None,
            "primary_label": "Eligible",
            "lifecycle_state": "READY",
            "permanent_blockers": [],
            "temporary_blockers": [],
        },
    )


@pytest.fixture(autouse=True)
def _execution_guard_allow_send(monkeypatch):
    from src.core.execution_guard import ExecutionGuardDecision, RESULT_ALLOW

    def _allow(*_a, **kw):
        action = _a[0] if _a else kw.get("action_type", "telegram_send")
        return ExecutionGuardDecision(
            allowed=True,
            action=str(action),
            result=RESULT_ALLOW,
            reason_code="test_bypass",
            message="test bypass",
        )

    monkeypatch.setattr("src.core.execution_guard.can_execute_action", _allow)


@pytest.fixture
def idem_db(monkeypatch):
    from src.core import database as database_module

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(database_module, "engine", engine)
    monkeypatch.setattr(database_module, "SessionLocal", TestSession)
    return TestSession


def _seed_pending_job(SessionFactory):
    db = SessionFactory()
    acc = Account(phone_number="+19995550123", status=AccountStatus.ACTIVE)
    tgt = ChatTarget(chat_type="group", title="g", username="testchan", tg_id=1001)
    db.add(acc)
    db.add(tgt)
    db.flush()
    binding = AccountTargetBinding(account_id=acc.id, target_id=tgt.id, can_post=True)
    db.add(binding)
    db.flush()
    job = ScheduledJob(
        account_id=acc.id,
        target_id=tgt.id,
        type=MessageType.PROMO.value,
        run_at=datetime(2026, 5, 8, 12, 0, 0),
        status=JobStatus.PENDING.value,
    )
    db.add(job)
    db.commit()
    jid, aid, tid, bid = int(job.id), int(acc.id), int(tgt.id), int(binding.id)
    db.close()
    return jid, aid, tid, bid


def test_reconcile_false_no_delivery(idem_db):
    SessionFactory = idem_db
    jid, *_ = _seed_pending_job(SessionFactory)
    assert executor_mod._reconcile_job_if_sent_delivery_exists(jid) is False


def test_reconcile_false_only_failed_delivery(idem_db):
    SessionFactory = idem_db
    jid, aid, tid, _ = _seed_pending_job(SessionFactory)
    db = SessionFactory()
    db.add(
        MessageDelivery(
            job_id=jid,
            account_id=aid,
            target_id=tid,
            type=MessageType.PROMO.value,
            status=DeliveryStatus.FAILED,
            error_message="x",
        )
    )
    db.commit()
    db.close()
    assert executor_mod._reconcile_job_if_sent_delivery_exists(jid) is False


@pytest.mark.parametrize("status_str", ["SENT", "sent", " Sent "])
def test_reconcile_true_sent_variants_reconcile_job(idem_db, status_str):
    SessionFactory = idem_db
    jid, aid, tid, _ = _seed_pending_job(SessionFactory)
    db = SessionFactory()
    db.add(
        MessageDelivery(
            job_id=jid,
            account_id=aid,
            target_id=tid,
            type=MessageType.PROMO.value,
            status=status_str,
            sent_at=datetime.utcnow(),
            tg_message_id=424242,
        )
    )
    db.commit()
    db.close()

    assert executor_mod._reconcile_job_if_sent_delivery_exists(jid) is True

    v = SessionFactory()
    row = v.query(ScheduledJob).filter(ScheduledJob.id == jid).one()
    assert row.status == JobStatus.SENT.value
    v.close()


@pytest.mark.asyncio
async def test_execute_job_skips_client_when_sent_delivery_exists(idem_db, monkeypatch):
    SessionFactory = idem_db
    jid, aid, tid, bid = _seed_pending_job(SessionFactory)

    def _pace_allow(*_a, **_k):
        return {
            "allowed": True,
            "retry_after_sec": 0,
            "reasons": [],
            "next_allowed_at": None,
            "next_allowed_at_dt": None,
        }

    monkeypatch.setattr("src.scheduler.pacing.get_send_pacing_decision", _pace_allow)
    monkeypatch.setattr(
        "src.scheduler.runtime_preflight.classify_scheduler_runtime_gate",
        lambda db, account: {
            "action": "proceed",
            "primary_blocker": None,
            "lifecycle_state": "OPERATIONAL",
            "permanent_blockers": [],
            "temporary_blockers": [],
        },
    )

    db = SessionFactory()
    db.add(
        MessageDelivery(
            job_id=jid,
            account_id=aid,
            target_id=tid,
            type=MessageType.PROMO.value,
            status="SENT",
            sent_at=datetime.utcnow(),
            tg_message_id=999001,
        )
    )
    db.commit()
    db.close()

    # Minimal template resolution: patch _resolve_template_for_job to avoid template DB fan-out
    monkeypatch.setattr(
        executor_mod,
        "_resolve_template_for_job",
        lambda *a, **k: ("hello {account_name}", None),
    )

    mock_add = AsyncMock()
    monkeypatch.setattr(executor_mod.client_manager, "add_account", mock_add)

    ok = await executor_mod.execute_job(jid)
    assert ok is True
    mock_add.assert_not_called()
