"""
Telegram gateway queue: DB enqueue, worker helpers, client timeout behavior.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.database import Base
import src.core.models  # noqa: F401
import src.telegram_gateway.models  # noqa: F401
from config.settings import settings
from src.telegram_gateway import errors as gw_errors
from src.telegram_gateway.client import TelegramGatewayClient
from src.telegram_gateway.service import (
    enqueue_job,
    get_job,
    mark_job_done,
    mark_job_failed,
    reset_stale_running_jobs,
)
from src.telegram_gateway.worker import _dedupe_messages


@pytest.fixture
def gw_db_ctx(monkeypatch):
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
    return Session


def test_dedupe_messages_by_telegram_id():
    rows = [
        {"telegram_message_id": 1, "text": "a"},
        {"telegram_message_id": 1, "text": "b"},
        {"telegram_message_id": 2, "text": "c"},
    ]
    out = _dedupe_messages(rows)
    assert len(out) == 2
    assert out[0]["telegram_message_id"] == 1
    assert out[1]["telegram_message_id"] == 2


def test_enqueue_send_and_mark_done(gw_db_ctx):
    jid = enqueue_job(
        account_id=7,
        task_type="send_message",
        target="@peer",
        payload={"text": "hi"},
    )
    row = get_job(jid)
    assert row is not None
    assert row.status == "pending"
    assert row.account_id == 7

    from src.telegram_gateway.service import get_db_context

    with get_db_context() as db:
        mark_job_done(
            db,
            jid,
            {"ok": True, "telegram_message_id": 42},
        )
    done = get_job(jid)
    assert done.status == "done"
    assert done.result_json["telegram_message_id"] == 42


def test_mark_failed_retries_then_dead(gw_db_ctx):
    jid = enqueue_job(
        account_id=1,
        task_type="fetch_messages",
        target="@x",
        payload={"limit": 5},
    )
    from src.telegram_gateway.service import get_db_context

    for _ in range(10):
        with get_db_context() as db:
            mark_job_failed(
                db,
                jid,
                error_code="session_lock_timeout",
                error_message="busy",
                retry_at=datetime.utcnow() + timedelta(seconds=1),
                increment_attempts=True,
            )
        st = get_job(jid).status
        if st == "failed":
            break
    assert get_job(jid).status == "failed"


def test_client_wait_timeout(monkeypatch, gw_db_ctx):
    jid = enqueue_job(
        account_id=1,
        task_type="send_message",
        target="@z",
        payload={"text": "x"},
    )
    c = TelegramGatewayClient()
    out = c.wait_for_job(jid, timeout_sec=0.15, poll_sec=0.05)
    assert out.get("error_code") == gw_errors.GATEWAY_TIMEOUT
    assert out.get("transient") is True


def test_gateway_timeout_is_transient():
    from src.ai_agent.transient_errors import is_transient_telegram_error

    assert is_transient_telegram_error("gateway_timeout", "") is True


def test_mark_done_overwrites_result_json(gw_db_ctx):
    """Second mark_job_done updates stored result (worker should only complete once)."""
    jid = enqueue_job(
        account_id=2,
        task_type="send_message",
        target="@u",
        payload={"text": "one"},
    )
    from src.telegram_gateway.service import get_db_context

    with get_db_context() as db:
        mark_job_done(db, jid, {"ok": True, "telegram_message_id": 99})
    with get_db_context() as db:
        mark_job_done(db, jid, {"ok": True, "telegram_message_id": 100})
    row = get_job(jid)
    assert row.result_json.get("telegram_message_id") == 100


def test_reset_stale_running_old_job_to_retry(gw_db_ctx):
    jid = enqueue_job(
        account_id=11,
        task_type="send_message",
        target="@stale",
        payload={"text": "x"},
    )
    from src.telegram_gateway.service import get_db_context
    from src.telegram_gateway.models import TelegramGatewayJob

    with get_db_context() as db:
        row = db.get(TelegramGatewayJob, jid)
        row.status = "running"
        row.updated_at = datetime.utcnow() - timedelta(seconds=500)
    with get_db_context() as db:
        n = reset_stale_running_jobs(db, older_than_sec=120)
    assert n == 1
    row2 = get_job(jid)
    assert row2.status == "retry"
    assert row2.error_code == "stale_running_recovered"


def test_reset_stale_running_skips_recent(gw_db_ctx):
    jid = enqueue_job(
        account_id=12,
        task_type="send_message",
        target="@fresh",
        payload={"text": "y"},
    )
    from src.telegram_gateway.service import get_db_context
    from src.telegram_gateway.models import TelegramGatewayJob

    with get_db_context() as db:
        row = db.get(TelegramGatewayJob, jid)
        row.status = "running"
        row.updated_at = datetime.utcnow()
    with get_db_context() as db:
        n = reset_stale_running_jobs(db, older_than_sec=120)
    assert n == 0
    assert get_job(jid).status == "running"


def test_single_sender_gateway_never_calls_direct(monkeypatch):
    monkeypatch.setenv("AI_AGENT_USE_TELEGRAM_GATEWAY", "true")
    monkeypatch.setattr(settings, "ai_agent_account_phones", "")
    monkeypatch.setattr(settings, "ai_agent_account_ids", "")
    import src.ai_agent.telegram_single_sender as mod

    def _allow(*_a, **_k):
        return None

    monkeypatch.setattr("src.core.execution_guard.require_execution_allowed", _allow)

    mod._gateway_enabled_logged = False

    class FakeGw:
        def enqueue_send_message(self, account_id, target, text):
            return 101

        def enqueue_fetch_messages(self, account_id, target, limit=20):
            return 102

        def wait_for_job(self, job_id, timeout_sec=None, poll_sec=0.01):
            if int(job_id) == 101:
                return {"ok": True, "telegram_message_id": 9}
            return {"ok": True, "messages": []}

    async def direct_forbidden(*args, **kwargs):
        raise AssertionError("TelegramDirectTransport must not run when gateway enabled")

    monkeypatch.setattr(
        mod.TelegramDirectTransport, "send_message_async", direct_forbidden
    )
    monkeypatch.setattr(
        mod.TelegramDirectTransport, "fetch_recent_messages_async", direct_forbidden
    )

    sender = mod.TelegramSingleSender()
    monkeypatch.setattr(mod, "_ai_agent_account_gate_passes", lambda _aid: True)
    monkeypatch.setattr(sender, "_gw", lambda: FakeGw())

    send_out = sender.send_message(1, "@u", "hi")
    assert send_out.get("ok") is True
    fetch_out = sender.fetch_recent_messages(1, "@u", 5)
    assert fetch_out.get("ok") is True


def test_single_sender_direct_path_uses_transport(monkeypatch):
    monkeypatch.setenv("AI_AGENT_USE_TELEGRAM_GATEWAY", "false")
    monkeypatch.setattr(settings, "ai_agent_account_phones", "")
    monkeypatch.setattr(settings, "ai_agent_account_ids", "")
    import src.ai_agent.telegram_single_sender as mod

    def _allow(*_a, **_k):
        return None

    monkeypatch.setattr("src.core.execution_guard.require_execution_allowed", _allow)

    mod._gateway_enabled_logged = False

    async def fake_send(self, account_id, target, text):
        return {
            "ok": True,
            "telegram_message_id": 55,
            "error_code": None,
            "error_message": None,
        }

    monkeypatch.setattr(mod.TelegramDirectTransport, "send_message_async", fake_send)

    sender = mod.TelegramSingleSender()
    monkeypatch.setattr(mod, "_ai_agent_account_gate_passes", lambda _aid: True)
    out = sender.send_message(2, "@peer", "x")
    assert out["ok"] is True
    assert out.get("telegram_message_id") == 55


def test_fetch_result_deduped_in_worker_contract():
    """Contract: done payload messages are deduped by worker before mark_job_done."""
    msgs = [
        {"telegram_message_id": 5, "text": "a"},
        {"telegram_message_id": 5, "text": "b"},
    ]
    assert len(_dedupe_messages(msgs)) == 1


def test_gateway_run_send_releases_account_in_finally(monkeypatch):
    """After each send job, worker disconnects pooled Telethon client (session lock)."""
    from src.telegram_gateway import worker as gw_worker

    removed: list[int] = []

    async def fake_send(account_id, target, text, **kwargs):
        return {
            "ok": True,
            "telegram_message_id": 42,
            "error_code": None,
            "error_message": None,
        }

    async def fake_remove(aid: int) -> bool:
        removed.append(int(aid))
        return True

    transport = MagicMock()
    transport.send_message_async = AsyncMock(side_effect=fake_send)
    monkeypatch.setattr(gw_worker, "_transport", transport)
    monkeypatch.setattr(gw_worker, "mark_job_done", Mock())
    monkeypatch.setattr(gw_worker, "mark_job_failed", Mock())
    monkeypatch.setattr(gw_worker, "get_db_context", lambda: nullcontext(MagicMock()))
    monkeypatch.setattr(
        gw_worker.client_manager, "remove_account", AsyncMock(side_effect=fake_remove)
    )

    asyncio.run(gw_worker._run_send(501, 77, "@peer", {"text": "hi"}, 0))
    assert removed == [77]
    transport.send_message_async.assert_awaited_once()


def test_gateway_run_fetch_releases_account_after_transport_exception(monkeypatch):
    from src.telegram_gateway import worker as gw_worker

    removed: list[int] = []

    async def fake_fetch(account_id, target, limit):
        raise RuntimeError("simulated transport failure")

    async def fake_remove(aid: int) -> bool:
        removed.append(int(aid))
        return True

    transport = MagicMock()
    transport.fetch_recent_messages_async = AsyncMock(side_effect=fake_fetch)
    monkeypatch.setattr(gw_worker, "_transport", transport)
    monkeypatch.setattr(gw_worker, "mark_job_done", Mock())
    monkeypatch.setattr(gw_worker, "mark_job_failed", Mock())
    monkeypatch.setattr(gw_worker, "get_db_context", lambda: nullcontext(MagicMock()))
    monkeypatch.setattr(
        gw_worker.client_manager, "remove_account", AsyncMock(side_effect=fake_remove)
    )

    with pytest.raises(RuntimeError, match="simulated transport failure"):
        asyncio.run(gw_worker._run_fetch(502, 88, "@peer", {"limit": 5}, 0))
    assert removed == [88]


def test_release_gateway_session_skips_when_account_id_none(monkeypatch):
    from src.telegram_gateway import worker as gw_worker

    mock_remove = AsyncMock(return_value=True)
    monkeypatch.setattr(gw_worker.client_manager, "remove_account", mock_remove)

    async def _go() -> None:
        await gw_worker._release_gateway_telethon_session(
            None, job_id=1, operation="send_message"
        )

    asyncio.run(_go())
    mock_remove.assert_not_awaited()
