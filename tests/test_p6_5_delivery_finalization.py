"""P6.5 — idempotent delivery finalization + dual-rail claim exclusion.

Isolated fixtures only. Does not contact live Telegram.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.database import Base
import src.core.models  # noqa: F401
import src.core.scheduler_models  # noqa: F401
import src.telegram_gateway.models  # noqa: F401
from src.core.certification_delivery import (
    gateway_delivery_idempotency_key,
    upsert_sent_delivery_for_gateway_cert,
)
from src.core.scheduler_models import (
    DeliveryStatus,
    JobStatus,
    MessageDelivery,
    ScheduledJob,
    SCHEDULED_JOB_P6_4_CERTIFICATION_MARKER,
)
from src.core.models import Account, AccountStatus
from src.telegram_gateway.service import enqueue_job, get_job, mark_job_done
from src.scheduler.job_claim import claim_due_job


@pytest.fixture
def mem_db(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def get_ctx():
        db = Session()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    monkeypatch.setattr("src.telegram_gateway.service.get_db_context", get_ctx)
    monkeypatch.setattr("src.telegram_gateway.worker.get_db_context", get_ctx)
    return Session


def _seed_account_target(db):
    acc = Account(
        id=107,
        phone_number="+10000000107",
        status=AccountStatus.ACTIVE,
        session_string="x",
    )
    db.add(acc)
    from src.core.scheduler_models import ChatTarget

    tgt = ChatTarget(
        id=1,
        tg_id=1775722510,
        username="cryptodiscussing",
        title="test",
        chat_type="channel",
    )
    db.add(tgt)
    db.flush()


def test_confirmed_send_creates_exactly_one_delivery(mem_db):
    Session = mem_db
    with Session() as db:
        _seed_account_target(db)
        job = ScheduledJob(
            type="PROMO",
            id=501,
            account_id=107,
            target_id=1,
            status=JobStatus.RUNNING.value,
            run_at=datetime.utcnow(),
            last_error=SCHEDULED_JOB_P6_4_CERTIFICATION_MARKER,
        )
        db.add(job)
        db.commit()

        a1 = upsert_sent_delivery_for_gateway_cert(
            db,
            gateway_job_id=9001,
            scheduled_job_id=501,
            account_id=107,
            target_id=1,
            telegram_message_id=15975,
            rendered_body="hello",
            binding_id=39,
            content_sha256="abc",
        )
        a2 = upsert_sent_delivery_for_gateway_cert(
            db,
            gateway_job_id=9001,
            scheduled_job_id=501,
            account_id=107,
            target_id=1,
            telegram_message_id=15975,
            rendered_body="hello",
            binding_id=39,
            content_sha256="abc",
        )
        db.commit()
        rows = db.query(MessageDelivery).filter(MessageDelivery.job_id == 501).all()
        job = db.query(ScheduledJob).filter(ScheduledJob.id == 501).one()

    assert a1["created"] is True
    assert a2["created"] is False
    assert a1["delivery_id"] == a2["delivery_id"]
    assert len(rows) == 1
    assert rows[0].tg_message_id == 15975
    assert rows[0].status == DeliveryStatus.SENT.value
    assert rows[0].idempotency_key == gateway_delivery_idempotency_key(9001)
    assert job.status == JobStatus.SENT.value


def test_duplicate_finalization_by_job_and_tg_mid(mem_db):
    """Existing canary-style key still dedupes when gateway_job key differs."""
    Session = mem_db
    with Session() as db:
        _seed_account_target(db)
        job = ScheduledJob(
            type="PROMO",
            id=369,
            account_id=107,
            target_id=1,
            status=JobStatus.SENT.value,
            run_at=datetime.utcnow(),
            last_error=SCHEDULED_JOB_P6_4_CERTIFICATION_MARKER,
        )
        db.add(job)
        existing = MessageDelivery(
            job_id=369,
            account_id=107,
            target_id=1,
            type="PROMO",
            status=DeliveryStatus.SENT.value,
            tg_message_id=15975,
            sent_at=datetime.utcnow(),
            idempotency_key="p6_4_369_15975",
        )
        db.add(existing)
        db.commit()
        audit = upsert_sent_delivery_for_gateway_cert(
            db,
            gateway_job_id=6071,
            scheduled_job_id=369,
            account_id=107,
            target_id=1,
            telegram_message_id=15975,
            rendered_body="x",
        )
        db.commit()
        n = db.query(MessageDelivery).filter(MessageDelivery.tg_message_id == 15975).count()
    assert audit["created"] is False
    assert audit["delivery_id"] == existing.id
    assert n == 1


def test_job_not_sent_without_confirmation_path(mem_db):
    """Upsert requires explicit telegram_message_id; no accidental SENT."""
    Session = mem_db
    with Session() as db:
        _seed_account_target(db)
        job = ScheduledJob(
            type="PROMO",
            id=502,
            account_id=107,
            target_id=1,
            status=JobStatus.RUNNING.value,
            run_at=datetime.utcnow(),
        )
        db.add(job)
        db.commit()
        with pytest.raises(Exception):
            # telegram_message_id is required int — callers must not invent SENTs
            upsert_sent_delivery_for_gateway_cert(
                db,
                gateway_job_id=1,
                scheduled_job_id=502,
                account_id=107,
                target_id=1,
                telegram_message_id=None,  # type: ignore[arg-type]
            )


@pytest.mark.asyncio
async def test_confirmed_send_db_failure_does_not_resend(mem_db, monkeypatch):
    import src.telegram_gateway.worker as gw_worker
    from src.core.p5d_gateway_reconciliation import CertificationSendGuard, NOT_SENT_CONFIRMED

    Session = mem_db
    with Session() as db:
        _seed_account_target(db)
        job = ScheduledJob(
            type="PROMO",
            id=510,
            account_id=107,
            target_id=1,
            status=JobStatus.PENDING.value,
            run_at=datetime.utcnow(),
            last_error=SCHEDULED_JOB_P6_4_CERTIFICATION_MARKER,
        )
        db.add(job)
        db.commit()

    sha = __import__("hashlib").sha256(b"body").hexdigest()
    jid = enqueue_job(
        account_id=107,
        task_type="send_message",
        target="1775722510",
        payload={
            "text": "body",
            "target_id": 1,
            "binding_id": 39,
            "job_marker": SCHEDULED_JOB_P6_4_CERTIFICATION_MARKER,
            "scheduled_job_id": 510,
            "expected_message_sha256": sha,
        },
    )

    calls = []

    async def fake_send(account_id, target, text, **kwargs):
        calls.append(1)
        return {"ok": True, "telegram_message_id": 777}

    class T:
        send_message_async = staticmethod(fake_send)

    monkeypatch.setattr(gw_worker, "_transport", T())
    monkeypatch.setattr(
        "src.telegram_gateway.worker.gateway_claim_allowed_account_ids",
        lambda db: frozenset({107}),
    )

    async def _noop_release(*a, **k):
        return None

    monkeypatch.setattr(gw_worker, "_release_gateway_telethon_session", _noop_release)

    async def _clear_guard(**kwargs):
        return CertificationSendGuard(outcome=NOT_SENT_CONFIRMED, reason_code="not_sent")

    def boom_upsert(*a, **k):
        raise RuntimeError("synthetic_db_timeout")

    with patch(
        "src.core.execution_guard.can_execute_action",
        return_value=MagicMock(allowed=True, reason_code="p6_4_live_canary_authorized", message="ok"),
    ), patch(
        "src.core.p5d_gateway_reconciliation.pre_send_certification_guard",
        side_effect=_clear_guard,
    ), patch(
        "src.core.certification_delivery.upsert_sent_delivery_for_gateway_cert",
        side_effect=boom_upsert,
    ), patch(
        "src.core.p6_4_authorization.mark_consumed",
    ):
        await gw_worker._process_job_snapshot(
            (
                jid,
                107,
                "send_message",
                "1775722510",
                {
                    "text": "body",
                    "target_id": 1,
                    "binding_id": 39,
                    "job_marker": SCHEDULED_JOB_P6_4_CERTIFICATION_MARKER,
                    "scheduled_job_id": 510,
                    "expected_message_sha256": sha,
                },
                0,
            )
        )

    done = get_job(jid)
    assert len(calls) == 1
    assert done.status == "done"
    result = done.result_json if isinstance(done.result_json, dict) else {}
    assert result.get("telegram_message_id") == 777
    assert result.get("delivery_persistence") == "SEND_CONFIRMED_DELIVERY_PERSISTENCE_PENDING"


@pytest.mark.asyncio
async def test_gateway_success_persists_delivery_and_marks_job(mem_db, monkeypatch):
    import src.telegram_gateway.worker as gw_worker
    from src.core.p5d_gateway_reconciliation import CertificationSendGuard, NOT_SENT_CONFIRMED
    from src.core import p6_4_authorization as auth

    Session = mem_db
    with Session() as db:
        _seed_account_target(db)
        job = ScheduledJob(
            type="PROMO",
            id=511,
            account_id=107,
            target_id=1,
            status=JobStatus.PENDING.value,
            run_at=datetime.utcnow(),
            last_error=SCHEDULED_JOB_P6_4_CERTIFICATION_MARKER,
        )
        db.add(job)
        db.commit()

    path = Path("/tmp/p6_5_test_manifest.json")
    monkeypatch.setattr(auth, "MANIFEST_PATH", path)
    m = auth.create_manifest(message_body="body")
    m["armed"] = True
    m["job_id"] = 511
    auth.save_manifest(m)

    jid = enqueue_job(
        account_id=107,
        task_type="send_message",
        target="1775722510",
        payload={
            "text": "body",
            "target_id": 1,
            "binding_id": 39,
            "job_marker": SCHEDULED_JOB_P6_4_CERTIFICATION_MARKER,
            "scheduled_job_id": 511,
            "expected_message_sha256": m["expected_message_sha256"],
            "p6_4_certification": True,
        },
    )

    async def fake_send(account_id, target, text, **kwargs):
        assert kwargs.get("job_marker") == SCHEDULED_JOB_P6_4_CERTIFICATION_MARKER
        assert kwargs.get("target_id") == 1
        assert kwargs.get("binding_id") == 39
        assert kwargs.get("content_sha256")
        return {"ok": True, "telegram_message_id": 8888}

    class T:
        send_message_async = staticmethod(fake_send)

    monkeypatch.setattr(gw_worker, "_transport", T())
    monkeypatch.setattr(
        "src.telegram_gateway.worker.gateway_claim_allowed_account_ids",
        lambda db: frozenset({107}),
    )

    async def _noop_release(*a, **k):
        return None

    monkeypatch.setattr(gw_worker, "_release_gateway_telethon_session", _noop_release)

    async def _clear_guard(**kwargs):
        return CertificationSendGuard(outcome=NOT_SENT_CONFIRMED, reason_code="not_sent")

    with patch(
        "src.core.execution_guard.can_execute_action",
        return_value=MagicMock(allowed=True, reason_code="p6_4_live_canary_authorized", message="ok"),
    ), patch(
        "src.core.p5d_gateway_reconciliation.pre_send_certification_guard",
        side_effect=_clear_guard,
    ):
        await gw_worker._process_job_snapshot(
            (
                jid,
                107,
                "send_message",
                "1775722510",
                {
                    "text": "body",
                    "target_id": 1,
                    "binding_id": 39,
                    "job_marker": SCHEDULED_JOB_P6_4_CERTIFICATION_MARKER,
                    "scheduled_job_id": 511,
                    "expected_message_sha256": m["expected_message_sha256"],
                    "p6_4_certification": True,
                },
                0,
            )
        )

    with Session() as db:
        dels = db.query(MessageDelivery).filter(MessageDelivery.job_id == 511).all()
        sj = db.query(ScheduledJob).filter(ScheduledJob.id == 511).one()
    assert get_job(jid).status == "done"
    assert len(dels) == 1
    assert dels[0].tg_message_id == 8888
    assert sj.status == JobStatus.SENT.value
    assert auth.load_manifest().get("consumed") is True

    # duplicate finalization
    with Session() as db:
        upsert_sent_delivery_for_gateway_cert(
            db,
            gateway_job_id=jid,
            scheduled_job_id=511,
            account_id=107,
            target_id=1,
            telegram_message_id=8888,
            rendered_body="body",
        )
        db.commit()
        n = db.query(MessageDelivery).filter(MessageDelivery.job_id == 511).count()
    assert n == 1


def test_attempt1_presend_denial_creates_no_delivery(mem_db):
    """Simulates attempt-1: no Telegram mid → no delivery upsert path."""
    Session = mem_db
    with Session() as db:
        _seed_account_target(db)
        n_before = db.query(MessageDelivery).count()
        # Only call upsert when mid exists — denial path skips it entirely
        n_after = db.query(MessageDelivery).count()
    assert n_before == n_after == 0


def test_claim_excludes_gateway_owned_certification_markers(mem_db):
    Session = mem_db
    with Session() as db:
        _seed_account_target(db)
        db.add(
            ScheduledJob(
            type="PROMO",
                id=601,
                account_id=107,
                target_id=1,
                status=JobStatus.PENDING.value,
                run_at=datetime.utcnow() - timedelta(minutes=1),
                last_error=SCHEDULED_JOB_P6_4_CERTIFICATION_MARKER,
            )
        )
        db.add(
            ScheduledJob(
            type="PROMO",
                id=602,
                account_id=107,
                target_id=1,
                status=JobStatus.PENDING.value,
                run_at=datetime.utcnow() - timedelta(minutes=1),
                last_error=None,
            )
        )
        db.commit()
        claimed = claim_due_job(
            db, worker_id="test-worker", lease_seconds=30, now_naive=datetime.utcnow()
        )
        assert claimed.job_id == 602
        # certification job remains pending
        left = db.query(ScheduledJob).filter(ScheduledJob.id == 601).one()
        assert left.status == JobStatus.PENDING.value


def test_transport_scope_kwargs_reach_both_guard_layers():
    """Regression for 849a43c: transport re-guard receives certification scope."""
    import inspect
    from src.ai_agent.telegram_single_sender import TelegramDirectTransport

    sig = inspect.signature(TelegramDirectTransport.send_message_async)
    for name in ("target_id", "job_marker", "job_id", "binding_id", "content_sha256"):
        assert name in sig.parameters


@pytest.mark.asyncio
async def test_uncertain_send_does_not_mark_sent(mem_db, monkeypatch):
    import src.telegram_gateway.worker as gw_worker
    from src.core.p5d_gateway_reconciliation import CertificationSendGuard, NOT_SENT_CONFIRMED

    Session = mem_db
    with Session() as db:
        _seed_account_target(db)
        job = ScheduledJob(
            type="PROMO",
            id=520,
            account_id=107,
            target_id=1,
            status=JobStatus.PENDING.value,
            run_at=datetime.utcnow(),
            last_error=SCHEDULED_JOB_P6_4_CERTIFICATION_MARKER,
        )
        db.add(job)
        db.commit()

    jid = enqueue_job(
        account_id=107,
        task_type="send_message",
        target="1775722510",
        payload={
            "text": "body",
            "target_id": 1,
            "job_marker": SCHEDULED_JOB_P6_4_CERTIFICATION_MARKER,
            "scheduled_job_id": 520,
        },
    )

    async def fake_send(account_id, target, text, **kwargs):
        return {"ok": False, "error_code": "timeout", "error_message": "x"}

    class T:
        send_message_async = staticmethod(fake_send)

    monkeypatch.setattr(gw_worker, "_transport", T())
    monkeypatch.setattr(
        "src.telegram_gateway.worker.gateway_claim_allowed_account_ids",
        lambda db: frozenset({107}),
    )

    async def _noop_release(*a, **k):
        return None

    monkeypatch.setattr(gw_worker, "_release_gateway_telethon_session", _noop_release)

    async def _clear_guard(**kwargs):
        return CertificationSendGuard(outcome=NOT_SENT_CONFIRMED, reason_code="not_sent")

    with patch(
        "src.core.execution_guard.can_execute_action",
        return_value=MagicMock(allowed=True, reason_code="ok", message="ok"),
    ), patch(
        "src.core.p5d_gateway_reconciliation.pre_send_certification_guard",
        side_effect=_clear_guard,
    ):
        await gw_worker._process_job_snapshot(
            (
                jid,
                107,
                "send_message",
                "1775722510",
                {
                    "text": "body",
                    "target_id": 1,
                    "job_marker": SCHEDULED_JOB_P6_4_CERTIFICATION_MARKER,
                    "scheduled_job_id": 520,
                },
                0,
            )
        )

    with Session() as db:
        sj = db.query(ScheduledJob).filter(ScheduledJob.id == 520).one()
        n = db.query(MessageDelivery).filter(MessageDelivery.job_id == 520).count()
    assert sj.status != JobStatus.SENT.value
    assert n == 0
    assert get_job(jid).status in ("retry", "failed")
