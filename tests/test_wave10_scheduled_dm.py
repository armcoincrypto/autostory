"""Wave 10 — Scheduled Direct Messages (isolated DB, fake Telegram transport)."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from src.core.database import Base
from src.core.models import Account, AccountStatus
from src.core.scheduler_models import JobStatus, MessageType, ScheduledJob
from src.messaging.models import STATUS_SENT, STATUS_UNCERTAIN, OwnerDmIntent
from src.messaging.owner_dm_service import OwnerDirectMessageService
from src.messaging.scheduled_dm_flags import scheduled_dm_create_allowed
from src.messaging.scheduled_dm_service import (
    ScheduledDirectMessageService,
    scheduled_dm_idempotency_key,
)
from src.messaging.transport import CountingFakeTransport
from src.scheduler.job_claim import claim_due_job
from src.scheduler.timezone import parse_owner_local_datetime

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


def _add_account(db, aid: int = 106, *, purpose="messaging"):
    a = Account(
        id=aid,
        phone_number=f"+1555000{aid:04d}",
        status=AccountStatus.ACTIVE,
        purpose=purpose,
        session_string="sess-test",
    )
    db.add(a)
    db.commit()
    return a


def _enable_schedule_flags():
    return patch.multiple(
        "src.messaging.scheduled_dm_flags",
        scheduled_dm_enabled=lambda: True,
    )


def _elig_ok():
    return SimpleNamespace(
        eligible=True,
        code="OK",
        reason="ok",
        to_dict=lambda: {"eligible": True, "code": "OK", "reason": "ok"},
    )


def _patch_db_context(session):
    @contextmanager
    def _ctx():
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise

    return patch("src.scheduler.executor.get_db_context", side_effect=lambda: _ctx())


# ── Architecture / regression surface ───────────────────────────────────────


def test_wave10_module_owners_exist():
    assert (ROOT / "src/messaging/scheduled_dm_service.py").is_file()
    assert (ROOT / "src/messaging/scheduled_dm_flags.py").is_file()
    assert MessageType.DM.value == "DM"
    assert JobStatus.UNCERTAIN.value == "UNCERTAIN"
    exec_src = (ROOT / "src/scheduler/executor.py").read_text(encoding="utf-8")
    assert "_execute_scheduled_dm_job" in exec_src
    assert "OwnerDirectMessageService" in exec_src
    assert "scheduled_dm_idempotency_key" in exec_src
    routes = (ROOT / "src/dashboard/messages_routes.py").read_text(encoding="utf-8")
    assert "/schedule" in routes
    assert "ScheduledDirectMessageService" in routes
    tpl = (ROOT / "src/dashboard/templates/messages.html").read_text(encoding="utf-8")
    assert "btn-schedule" in tpl
    assert "Schedule message" in tpl
    assert not (ROOT / "src/scheduler/dm_scheduler.py").exists()
    assert not (ROOT / "src/messaging/dm_scheduler_v2.py").exists()


def test_fail_closed_without_flags(memory_db):
    _add_account(memory_db)
    svc = ScheduledDirectMessageService()
    with patch("src.messaging.scheduled_dm_flags.scheduled_dm_enabled", return_value=False):
        assert scheduled_dm_create_allowed() is False
        r = svc.schedule(
            memory_db,
            account_id=106,
            peer_id="8531893204",
            message="hello",
            local_date="2026-09-10",
            local_time="15:00",
            timezone="Asia/Yerevan",
        )
        assert r.status_code == 423
        assert r.payload["error_code"] == "SCHEDULED_DM_DISABLED"
        assert memory_db.query(ScheduledJob).count() == 0


def test_wave_d_dm_allowed_without_global_scheduler_mutations(memory_db):
    """Wave D: SCHEDULED_DM_ENABLED alone is enough for DM create."""
    _add_account(memory_db)
    svc = ScheduledDirectMessageService()
    with patch("src.messaging.scheduled_dm_flags.scheduled_dm_enabled", return_value=True):
        with patch(
            "src.dashboard.scheduler_mutations.scheduler_mutations_enabled",
            return_value=False,
        ):
            assert scheduled_dm_create_allowed() is True
            with patch(
                "src.messaging.scheduled_dm_service.evaluate_dm_account_eligibility",
                return_value=_elig_ok(),
            ):
                r = svc.schedule(
                    memory_db,
                    account_id=106,
                    peer_id="8531893204",
                    message="wave-d-canary",
                    local_date="2026-09-10",
                    local_time="15:00",
                    timezone="Asia/Yerevan",
                )
            assert r.status_code == 200
            assert memory_db.query(ScheduledJob).filter(ScheduledJob.type == "DM").count() == 1


def test_yerevan_local_to_utc_and_create_pending(memory_db):
    _add_account(memory_db)
    fake = CountingFakeTransport()
    dm = OwnerDirectMessageService(transport=fake)
    svc = ScheduledDirectMessageService(dm_service=dm)
    instant = parse_owner_local_datetime("2026-09-10", "15:00", "Asia/Yerevan")
    assert instant.utc_naive.isoformat() == "2026-09-10T11:00:00"

    with _enable_schedule_flags():
        with patch(
            "src.messaging.scheduled_dm_service.evaluate_dm_account_eligibility",
            return_value=_elig_ok(),
        ):
            with patch(
                "src.messaging.owner_dm_service.evaluate_dm_account_eligibility",
                return_value=_elig_ok(),
            ):
                with patch(
                    "src.messaging.owner_dm_service.evaluate_owner_dm_rate_policy",
                    return_value={"allowed": True},
                ):
                    with patch(
                        "src.messaging.scheduled_dm_service.utc_now_naive",
                        return_value=datetime(2026, 9, 1, 0, 0, 0),
                    ):
                        r = svc.schedule(
                            memory_db,
                            account_id=106,
                            peer_id="8531893204",
                            message="Wave10 cert hello",
                            local_date="2026-09-10",
                            local_time="15:00",
                            timezone="Asia/Yerevan",
                        )
    assert r.ok, r.payload
    assert r.payload["scheduled_at_utc"] == "2026-09-10T11:00:00Z"
    assert "15:00" in (r.payload["scheduled_at_local"] or "")
    assert "Asia/Yerevan" in (r.payload["scheduled_at_local"] or "")
    job = memory_db.query(ScheduledJob).one()
    assert job.type == MessageType.DM.value
    assert job.status == JobStatus.PENDING.value
    assert job.target_id is None
    assert job.peer_id == "8531893204"
    assert job.message_body == "Wave10 cert hello"
    assert job.run_at == datetime(2026, 9, 10, 11, 0, 0)
    assert fake.send_calls == 0
    assert r.payload["idempotency_key"] == scheduled_dm_idempotency_key(job.id)


def test_past_time_rejected(memory_db):
    _add_account(memory_db)
    svc = ScheduledDirectMessageService()
    with _enable_schedule_flags():
        with patch(
            "src.messaging.scheduled_dm_service.evaluate_dm_account_eligibility",
            return_value=_elig_ok(),
        ):
            with patch(
                "src.messaging.scheduled_dm_service.utc_now_naive",
                return_value=datetime(2026, 9, 10, 12, 0, 0),
            ):
                r = svc.schedule(
                    memory_db,
                    account_id=106,
                    peer_id="8531893204",
                    message="too late",
                    local_date="2026-09-10",
                    local_time="15:00",
                    timezone="Asia/Yerevan",
                )
    assert r.status_code == 400
    assert r.payload["error_code"] == "SCHEDULE_IN_PAST"


def test_invalid_timezone_rejected(memory_db):
    _add_account(memory_db)
    svc = ScheduledDirectMessageService()
    with _enable_schedule_flags():
        with patch(
            "src.messaging.scheduled_dm_service.evaluate_dm_account_eligibility",
            return_value=_elig_ok(),
        ):
            r = svc.schedule(
                memory_db,
                account_id=106,
                peer_id="8531893204",
                message="x",
                local_date="2026-09-10",
                local_time="15:00",
                timezone="Not/AZone",
            )
    assert r.status_code == 400


def test_empty_message_rejected(memory_db):
    _add_account(memory_db)
    svc = ScheduledDirectMessageService()
    with _enable_schedule_flags():
        r = svc.schedule(
            memory_db,
            account_id=106,
            peer_id="8531893204",
            message="   ",
            local_date="2026-09-10",
            local_time="15:00",
            timezone="Asia/Yerevan",
        )
    assert r.status_code == 400
    assert r.payload["error_code"] == "EMPTY_MESSAGE"


@pytest.mark.asyncio
async def test_due_job_executes_via_owner_dm_once(memory_db):
    _add_account(memory_db)
    fake = CountingFakeTransport()
    run_at = datetime(2026, 9, 10, 11, 0, 0)
    now = run_at + timedelta(minutes=5)
    job = ScheduledJob(
        account_id=106,
        target_id=None,
        type=MessageType.DM.value,
        run_at=run_at,
        status=JobStatus.PENDING.value,
        peer_id="8531893204",
        peer_type="private",
        message_body="due send",
        schedule_timezone="Asia/Yerevan",
        created_at=run_at,
        updated_at=run_at,
    )
    memory_db.add(job)
    memory_db.commit()
    jid = int(job.id)
    key = scheduled_dm_idempotency_key(jid)

    claim = claim_due_job(
        memory_db, worker_id="w-test", lease_seconds=900, now_naive=now
    )
    assert claim.job_id == jid
    memory_db.commit()

    svc = OwnerDirectMessageService(transport=fake)
    with _patch_db_context(memory_db):
        with patch(
            "src.messaging.owner_dm_service.messages_execution_enabled",
            return_value=True,
        ):
            with patch(
                "src.messaging.owner_dm_service.require_execution_allowed",
                return_value=None,
            ):
                with patch(
                    "src.messaging.owner_dm_service.evaluate_dm_account_eligibility",
                    return_value=_elig_ok(),
                ):
                    with patch(
                        "src.messaging.owner_dm_service.evaluate_owner_dm_rate_policy",
                        return_value={"allowed": True},
                    ):
                        with patch(
                            "src.messaging.owner_dm_service.OwnerDirectMessageService",
                            return_value=svc,
                        ):
                            from src.scheduler.executor import execute_job

                            ok = await execute_job(jid)
    assert ok is True
    assert fake.send_calls == 1
    memory_db.expire_all()
    job2 = memory_db.query(ScheduledJob).filter(ScheduledJob.id == jid).one()
    assert job2.status == JobStatus.SENT.value
    intent = (
        memory_db.query(OwnerDmIntent)
        .filter(OwnerDmIntent.idempotency_key == key)
        .one()
    )
    assert intent.status == STATUS_SENT

    # Retry same RUNNING job — same idempotency key, no second Telegram send
    job2.status = JobStatus.RUNNING.value
    memory_db.commit()
    with _patch_db_context(memory_db):
        with patch(
            "src.messaging.owner_dm_service.messages_execution_enabled",
            return_value=True,
        ):
            with patch(
                "src.messaging.owner_dm_service.require_execution_allowed",
                return_value=None,
            ):
                with patch(
                    "src.messaging.owner_dm_service.OwnerDirectMessageService",
                    return_value=svc,
                ):
                    from src.scheduler.executor import execute_job

                    await execute_job(jid)
    assert fake.send_calls == 1


@pytest.mark.asyncio
async def test_uncertain_maps_job_status(memory_db):
    _add_account(memory_db)
    now = datetime(2026, 9, 10, 11, 0, 0)
    job = ScheduledJob(
        account_id=106,
        target_id=None,
        type=MessageType.DM.value,
        run_at=now,
        status=JobStatus.RUNNING.value,
        peer_id="8531893204",
        peer_type="private",
        message_body="uncertain path",
        schedule_timezone="Asia/Yerevan",
        created_at=now,
        updated_at=now,
        lease_owner="w1",
    )
    memory_db.add(job)
    memory_db.commit()
    jid = int(job.id)
    key = scheduled_dm_idempotency_key(jid)

    async def _unc_send(*_a, **_k):
        return {
            "ok": False,
            "status": "UNCERTAIN",
            "replay": True,
            "idempotency_key": key,
            "error_code": "AMBIGUOUS",
            "error_message": "persist failed after transport",
        }

    fake_svc = SimpleNamespace(send_now=AsyncMock(side_effect=_unc_send))
    with _patch_db_context(memory_db):
        with patch(
            "src.messaging.owner_dm_service.OwnerDirectMessageService",
            return_value=fake_svc,
        ):
            from src.scheduler.executor import _execute_scheduled_dm_job

            await _execute_scheduled_dm_job(jid)

    memory_db.expire_all()
    job2 = memory_db.query(ScheduledJob).filter(ScheduledJob.id == jid).one()
    assert job2.status == JobStatus.UNCERTAIN.value
    assert fake_svc.send_now.await_args.kwargs["idempotency_key"] == key


def test_cancel_pending_only(memory_db):
    _add_account(memory_db)
    now = datetime(2026, 9, 10, 11, 0, 0)
    pending = ScheduledJob(
        account_id=106,
        type=MessageType.DM.value,
        run_at=now,
        status=JobStatus.PENDING.value,
        peer_id="1",
        message_body="x",
        schedule_timezone="Asia/Yerevan",
    )
    sent = ScheduledJob(
        account_id=106,
        type=MessageType.DM.value,
        run_at=now,
        status=JobStatus.SENT.value,
        peer_id="1",
        message_body="y",
        schedule_timezone="Asia/Yerevan",
    )
    memory_db.add_all([pending, sent])
    memory_db.commit()
    svc = ScheduledDirectMessageService()
    with _enable_schedule_flags():
        r1 = svc.cancel(memory_db, job_id=pending.id)
        r2 = svc.cancel(memory_db, job_id=sent.id)
    assert r1.ok and r1.payload["status"] == "CANCELLED"
    assert r2.status_code == 409


def test_concurrent_claim_one_winner(memory_db):
    _add_account(memory_db)
    run_at = datetime(2026, 9, 10, 11, 0, 0)
    now = run_at + timedelta(minutes=1)
    job = ScheduledJob(
        account_id=106,
        type=MessageType.DM.value,
        run_at=run_at,
        status=JobStatus.PENDING.value,
        peer_id="1",
        message_body="x",
    )
    memory_db.add(job)
    memory_db.commit()
    c1 = claim_due_job(memory_db, worker_id="w1", lease_seconds=900, now_naive=now)
    memory_db.commit()
    c2 = claim_due_job(memory_db, worker_id="w2", lease_seconds=900, now_naive=now)
    memory_db.commit()
    assert c1.job_id == job.id
    assert c2.job_id is None


def test_schema_target_id_nullable_on_create_all(memory_db):
    cols = {c["name"]: c for c in inspect(memory_db.bind).get_columns("scheduled_jobs")}
    assert "peer_id" in cols
    assert "message_body" in cols
    assert cols["target_id"]["nullable"] is True


def test_no_moscow_hardcode_in_messages_schedule_ui():
    tpl = (ROOT / "src/dashboard/templates/messages.html").read_text(encoding="utf-8")
    assert "Europe/Moscow" not in tpl
    assert "Asia/Yerevan" in tpl


def test_regression_send_now_and_ai_draft_untouched():
    routes = (ROOT / "src/dashboard/messages_routes.py").read_text(encoding="utf-8")
    assert "def messages_send_now" in routes
    assert "def messages_draft" in routes
    assert "OwnerDirectMessageService" in routes
    gen = (ROOT / "src/scheduler/generation_eligibility.py").read_text(encoding="utf-8")
    assert "MessageType.DM" not in gen


def test_local_cert_proof_roundtrip():
    instant = parse_owner_local_datetime("2026-09-10", "15:00", "Asia/Yerevan")
    assert instant.utc_naive.isoformat() + "Z" == "2026-09-10T11:00:00Z"
    assert scheduled_dm_idempotency_key(42) == "scheduled-dm:42"
