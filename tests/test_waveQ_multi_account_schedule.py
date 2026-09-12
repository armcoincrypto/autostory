"""Wave Q — multi-account schedule comfort (no Telegram mutations)."""
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
from src.core.scheduler_models import JobStatus, MessageType, ScheduledJob
from src.messaging.multi_account_schedule import (
    MultiAccountScheduleOrchestrator,
    compute_spaced_slots,
    map_readiness_status,
    scheduled_ops_summary,
)
from src.messaging.peer_labels import resolve_owner_peer_label
from src.messaging.scheduled_dm_service import ScheduledDirectMessageService
from src.scheduler.timezone import SchedulerTimezoneError

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


def _add_account(db, aid: int, *, first_name: str = None, purpose="messaging"):
    a = Account(
        id=aid,
        phone_number=f"+1555000{aid:04d}",
        status=AccountStatus.ACTIVE,
        purpose=purpose,
        session_string="sess-test",
        first_name=first_name or f"Acct{aid}",
    )
    db.add(a)
    db.commit()
    return a


def _elig(ok=True, code="OK"):
    return SimpleNamespace(
        eligible=ok,
        code=code,
        reason=code,
        to_dict=lambda: {"eligible": ok, "code": code},
    )


def test_waveQ_no_new_execution_owners():
    orch = (ROOT / "src/messaging/multi_account_schedule.py").read_text(encoding="utf-8")
    assert "ScheduledDirectMessageService" in orch
    assert "create_bulk" in orch
    routes = (ROOT / "src/dashboard/messages_routes.py").read_text(encoding="utf-8")
    assert "schedule-bulk" in routes
    assert "account-matrix" in routes
    assert "cancel-bulk" in routes
    assert "MultiAccountScheduleOrchestrator" in routes
    tpl = (ROOT / "src/dashboard/templates/messages.html").read_text(encoding="utf-8")
    assert "mode-by-chat" in tpl
    assert "Select all ready" in tpl
    assert "btn-bulk-confirm" in tpl
    assert "No mass Send Now" in tpl
    assert "chat-mode" in tpl
    # Orchestrator must not call ODMS send directly
    assert "send_now" not in orch
    assert "OwnerDirectMessageService" not in orch


def test_map_readiness_statuses():
    assert map_readiness_status(eligible=False, eligibility_code="PROTECTED", preview=None)[1] == "Protected"
    assert map_readiness_status(eligible=False, eligibility_code="RESERVED", preview=None)[1] == "Reserved"
    assert map_readiness_status(eligible=False, eligibility_code="AUTH_FAILED", preview=None)[1] == "Needs login"
    assert map_readiness_status(eligible=False, eligibility_code="DISABLED", preview=None)[1] == "Disabled"
    ready = map_readiness_status(
        eligible=True,
        eligibility_code="OK",
        preview={"ok": True, "already_joined": True, "can_post": True},
    )
    assert ready == ("ready", "Ready", True)
    cannot = map_readiness_status(
        eligible=True,
        eligibility_code="OK",
        preview={"ok": True, "already_joined": True, "can_post": False},
    )
    assert cannot[0] == "cannot_post"
    assert cannot[2] is False
    wait = map_readiness_status(
        eligible=True,
        eligibility_code="OK",
        preview={"ok": True, "already_joined": False, "message": "Waiting for admin approval"},
    )
    assert wait[0] == "waiting_approval"
    assert wait[2] is False
    nj = map_readiness_status(
        eligible=True,
        eligibility_code="OK",
        preview={"ok": True, "already_joined": False, "message": "Not a member"},
    )
    assert nj[0] == "not_joined"
    assert nj[2] is False


def test_spacing_calculation_yerevan():
    slots = compute_spaced_slots(
        local_date="2026-09-13",
        local_time="10:00",
        timezone_name="Asia/Yerevan",
        count=4,
        spacing_sec=60,
    )
    assert len(slots) == 4
    assert slots[0]["local_time"].startswith("10:00")
    assert slots[1]["local_time"].startswith("10:01")
    assert slots[2]["local_time"].startswith("10:02")
    assert slots[3]["local_time"].startswith("10:03")
    assert "Asia/Yerevan" in slots[0]["scheduled_at_local"]
    with pytest.raises(SchedulerTimezoneError):
        compute_spaced_slots(
            local_date="2026-09-13",
            local_time="10:00",
            timezone_name="Asia/Yerevan",
            count=2,
            spacing_sec=45,
        )


def test_matrix_ready_and_protected(memory_db):
    _add_account(memory_db, 106, first_name="Rachael")
    _add_account(memory_db, 107, first_name="Krystal")
    _add_account(memory_db, 108, first_name="ProtectedOne")

    def elig(db, aid):
        if int(aid) == 108:
            return _elig(False, "PROTECTED")
        return _elig(True, "OK")

    responses = {
        106: {
            "ok": True,
            "already_joined": True,
            "can_post": True,
            "title": "Storyfleet Test Group",
            "chat_type": "supergroup",
            "peer_id": "-1004297144441",
        },
        107: {
            "ok": True,
            "already_joined": False,
            "message": "Not a member",
            "title": "Storyfleet Test Group",
            "chat_type": "supergroup",
            "peer_id": "-1004297144441",
        },
    }

    class FakeChat:
        async def preview_async(self, account_id, ref):
            return responses.get(
                int(account_id),
                {"ok": False, "message": "Temporarily unavailable"},
            )

    orch = MultiAccountScheduleOrchestrator(chat_service=FakeChat())

    def _run(coro):
        import asyncio

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)

    orch._run_async = _run

    with patch(
        "src.messaging.multi_account_schedule.evaluate_dm_account_eligibility",
        side_effect=elig,
    ):
        matrix = orch.build_matrix(
            memory_db,
            ref="https://t.me/+test",
            account_ids=[106, 107, 108],
            probe_telegram=True,
        )

    assert matrix["ok"] is True
    by_id = {r["account_id"]: r for r in matrix["accounts"]}
    assert by_id[106]["ready"] is True and by_id[106]["status_label"] == "Ready"
    assert by_id[107]["status_label"] == "Not joined" and by_id[107]["ready"] is False
    assert by_id[108]["status_label"] == "Protected" and by_id[108]["ready"] is False
    ready_ids = [r["account_id"] for r in matrix["accounts"] if r["ready"]]
    assert ready_ids == [106]


def test_bulk_schedule_n_jobs_spacing_and_idempotent(memory_db):
    _add_account(memory_db, 106, first_name="Rachael")
    _add_account(memory_db, 107, first_name="Krystal")
    future = (datetime.utcnow() + timedelta(days=3)).strftime("%Y-%m-%d")

    class FakeChat:
        async def preview_async(self, account_id, ref):
            return {
                "ok": True,
                "already_joined": True,
                "can_post": True,
                "title": "Storyfleet Test Group",
                "chat_type": "supergroup",
                "peer_id": "-1004297144441",
            }

    def _run(coro):
        import asyncio

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)

    orch = MultiAccountScheduleOrchestrator(chat_service=FakeChat())
    orch._run_async = _run

    with patch("src.messaging.scheduled_dm_flags.scheduled_dm_enabled", return_value=True):
        with patch(
            "src.messaging.chat_flags.messages_group_channel_send_enabled",
            return_value=True,
        ):
            with patch(
                "src.messaging.multi_account_schedule.evaluate_dm_account_eligibility",
                return_value=_elig(),
            ):
                with patch(
                    "src.messaging.scheduled_dm_service.evaluate_dm_account_eligibility",
                    return_value=_elig(),
                ):
                    preview = orch.preview_bulk(
                        memory_db,
                        ref="https://t.me/+test",
                        account_ids=[106, 107],
                        message="wave-q hello",
                        local_date=future,
                        local_time="10:00:00",
                        timezone_name="Asia/Yerevan",
                        spacing_sec=60,
                    )
                    assert preview.ok
                    assert preview.payload["jobs_to_create"] == 2
                    assert preview.payload["plan"][0]["local_time"].startswith("10:00")
                    assert preview.payload["plan"][1]["local_time"].startswith("10:01")

                    r1 = orch.create_bulk(
                        memory_db,
                        ref="https://t.me/+test",
                        account_ids=[106, 107],
                        message="wave-q hello",
                        local_date=future,
                        local_time="10:00:00",
                        timezone_name="Asia/Yerevan",
                        spacing_sec=60,
                        peer_type="supergroup",
                        idempotency_key="waveq-bulk-1",
                    )
                    assert r1.ok
                    assert r1.payload["created"] == 2
                    assert r1.payload["failed"] == 0
                    assert memory_db.query(ScheduledJob).filter(ScheduledJob.type == "DM").count() == 2

                    r2 = orch.create_bulk(
                        memory_db,
                        ref="https://t.me/+test",
                        account_ids=[106, 107],
                        message="wave-q hello",
                        local_date=future,
                        local_time="10:00:00",
                        timezone_name="Asia/Yerevan",
                        spacing_sec=60,
                        peer_type="supergroup",
                        idempotency_key="waveq-bulk-1",
                    )
                    assert r2.payload["created"] == 2
                    assert r2.payload.get("reused") == 2
                    assert memory_db.query(ScheduledJob).filter(ScheduledJob.type == "DM").count() == 2


def test_bulk_skips_non_ready(memory_db):
    _add_account(memory_db, 106, first_name="Rachael")
    _add_account(memory_db, 108, first_name="ProtectedOne")

    class FakeChat:
        async def preview_async(self, account_id, ref):
            return {
                "ok": True,
                "already_joined": True,
                "can_post": True,
                "title": "G",
                "chat_type": "supergroup",
                "peer_id": "-1004297144441",
            }

    def _run(coro):
        import asyncio

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)

    orch = MultiAccountScheduleOrchestrator(chat_service=FakeChat())
    orch._run_async = _run
    future = (datetime.utcnow() + timedelta(days=3)).strftime("%Y-%m-%d")

    def elig(db, aid):
        if int(aid) == 108:
            return _elig(False, "PROTECTED")
        return _elig(True)

    with patch("src.messaging.scheduled_dm_flags.scheduled_dm_enabled", return_value=True):
        with patch(
            "src.messaging.chat_flags.messages_group_channel_send_enabled",
            return_value=True,
        ):
            with patch(
                "src.messaging.multi_account_schedule.evaluate_dm_account_eligibility",
                side_effect=elig,
            ):
                with patch(
                    "src.messaging.scheduled_dm_service.evaluate_dm_account_eligibility",
                    return_value=_elig(),
                ):
                    r = orch.create_bulk(
                        memory_db,
                        ref="x",
                        account_ids=[106, 108],
                        message="skip protected",
                        local_date=future,
                        local_time="11:00:00",
                        timezone_name="Asia/Yerevan",
                        spacing_sec=30,
                        peer_type="supergroup",
                        idempotency_key="waveq-skip",
                    )
    assert r.ok
    assert r.payload["created"] == 1
    assert len(r.payload["skipped"]) == 1
    assert memory_db.query(ScheduledJob).count() == 1


def test_bulk_cancel_pending_not_sent(memory_db):
    _add_account(memory_db, 106)
    future = datetime.utcnow() + timedelta(days=2)
    pending = ScheduledJob(
        account_id=106,
        type=MessageType.DM.value,
        run_at=future,
        status=JobStatus.PENDING.value,
        peer_id="-1004297144441",
        peer_type="supergroup",
        message_body="p",
        schedule_timezone="Asia/Yerevan",
    )
    sent = ScheduledJob(
        account_id=106,
        type=MessageType.DM.value,
        run_at=future,
        status=JobStatus.SENT.value,
        peer_id="-1004297144441",
        peer_type="supergroup",
        message_body="s",
        schedule_timezone="Asia/Yerevan",
    )
    uncertain = ScheduledJob(
        account_id=106,
        type=MessageType.DM.value,
        run_at=future,
        status=JobStatus.UNCERTAIN.value,
        peer_id="-1004297144441",
        peer_type="supergroup",
        message_body="u",
        schedule_timezone="Asia/Yerevan",
        last_error="UNCERTAIN: check chat",
    )
    memory_db.add_all([pending, sent, uncertain])
    memory_db.commit()
    orch = MultiAccountScheduleOrchestrator()
    with patch("src.messaging.scheduled_dm_flags.scheduled_dm_enabled", return_value=True):
        r = orch.cancel_bulk(memory_db, job_ids=[pending.id, sent.id, uncertain.id])
    assert r.payload["cancelled"] == 1
    assert r.payload["rejected"] == 2
    memory_db.refresh(pending)
    memory_db.refresh(sent)
    memory_db.refresh(uncertain)
    assert pending.status == JobStatus.CANCELLED.value
    assert sent.status == JobStatus.SENT.value
    assert uncertain.status == JobStatus.UNCERTAIN.value


def test_uncertain_no_automatic_retry_in_executor_source():
    src = (ROOT / "src/scheduler/executor.py").read_text(encoding="utf-8")
    assert "UNCERTAIN" in src
    assert "Do NOT mint a new idempotency key" in src
    assert "job.status = JobStatus.UNCERTAIN.value" in src


def test_ops_summary_db_only(memory_db):
    _add_account(memory_db, 106)
    now = datetime.utcnow()
    memory_db.add(
        ScheduledJob(
            account_id=106,
            type=MessageType.DM.value,
            run_at=now + timedelta(hours=1),
            status=JobStatus.PENDING.value,
            peer_id="1",
            message_body="x",
            schedule_timezone="Asia/Yerevan",
        )
    )
    memory_db.add(
        ScheduledJob(
            account_id=106,
            type=MessageType.DM.value,
            run_at=now,
            status=JobStatus.UNCERTAIN.value,
            peer_id="1",
            message_body="y",
            schedule_timezone="Asia/Yerevan",
        )
    )
    memory_db.commit()
    summary = scheduled_ops_summary(memory_db, timezone_name="Asia/Yerevan")
    assert summary["ok"] is True
    assert summary["uncertain"] >= 1


def test_list_jobs_filters_and_peer_names(memory_db):
    from src.core.scheduler_models import ChatTarget

    _add_account(memory_db, 106, first_name="Rachael")
    memory_db.add(
        ChatTarget(
            tg_id=4297144441,
            title="Storyfleet Test Group",
            chat_type="supergroup",
        )
    )
    memory_db.commit()
    future = datetime.utcnow() + timedelta(days=1)
    memory_db.add(
        ScheduledJob(
            account_id=106,
            type=MessageType.DM.value,
            run_at=future,
            status=JobStatus.PENDING.value,
            peer_id="-1004297144441",
            peer_type="supergroup",
            message_body="hi",
            schedule_timezone="Asia/Yerevan",
        )
    )
    memory_db.commit()
    svc = ScheduledDirectMessageService()
    rows = svc.list_jobs(memory_db, peer="-1004297144441", status="UPCOMING", limit=10)
    assert len(rows) == 1
    label = resolve_owner_peer_label(memory_db, "-1004297144441", "supergroup")
    assert "4297144441" not in label or "Storyfleet" in label
    assert "Storyfleet Test Group" in label


def test_no_auto_send_after_approval_in_matrix():
    """Waiting approval is never Ready — cannot be selected for schedule."""
    key, label, ready = map_readiness_status(
        eligible=True,
        eligibility_code="OK",
        preview={"ok": True, "already_joined": False, "message": "Join requested — waiting for admin approval"},
    )
    assert key == "waiting_approval"
    assert ready is False
