"""Ops audit P1 — cancel atomicity, join skip, overdue skip, Messages JS gate."""
from __future__ import annotations

import subprocess
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.database import Base
from src.core.models import Account, AccountStatus
from src.core.scheduler_models import JobStatus, MessageType, ScheduledJob
from src.messaging.multi_account_join import MultiAccountJoinOrchestrator
from src.messaging.scheduled_dm_flags import scheduled_dm_create_allowed
from src.messaging.scheduled_dm_service import (
    MAX_OVERDUE_EXECUTE_SEC,
    ScheduledDirectMessageService,
)
from src.scheduler.executor import _execute_scheduled_dm_job

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


def _add_account(db, aid: int = 106):
    a = Account(
        id=aid,
        phone_number=f"+1555000{aid:04d}",
        status=AccountStatus.ACTIVE,
        purpose="messaging",
        session_string="sess-test",
        first_name=f"A{aid}",
    )
    db.add(a)
    db.commit()
    return a


def _elig():
    return SimpleNamespace(eligible=True, code="OK", reason="ok")


def _run_coro(coro):
    import asyncio

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


def test_messages_js_syntax_gate_passes_on_current_template():
    script = ROOT / "scripts" / "release" / "check_messages_js_syntax.sh"
    assert script.is_file()
    r = subprocess.run(
        ["bash", str(script), str(ROOT)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "messages_js_syntax=OK" in (r.stdout + r.stderr)


def test_messages_js_syntax_gate_fails_on_broken_try():
    """Prove malformed Messages JS blocks deploy (BROKEN_MESSAGES_JS_DEPLOY_BLOCKED)."""
    import re
    import tempfile

    html = (ROOT / "src/dashboard/templates/messages.html").read_text(encoding="utf-8")
    # Inject the historical bug: try without catch in init IIFE.
    broken = re.sub(
        r"\(async function init\(\)\s*\{[\s\S]*?\}\)\s*\(\s*\)\s*;",
        "(async function init() {\n    try {\n      await loadAccounts();\n    }\n  })();",
        html,
        count=1,
    )
    assert "catch" not in broken[broken.rfind("async function init") : broken.rfind("async function init") + 200]
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        tpl = root / "src/dashboard/templates"
        tpl.mkdir(parents=True)
        (tpl / "messages.html").write_text(broken, encoding="utf-8")
        script = ROOT / "scripts" / "release" / "check_messages_js_syntax.sh"
        r = subprocess.run(
            ["bash", str(script), str(root)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    assert r.returncode != 0, "broken Messages JS must fail the deploy gate"


def test_messages_init_isolates_panel_failures():
    src = (ROOT / "src/dashboard/templates/messages.html").read_text(encoding="utf-8")
    assert "each panel isolated" in src
    assert "Accounts failed to load" in src
    assert "data-retry=\"accounts\"" in src
    # Stale matrix cleared before Chat B load
    assert "Stale-state guard" in src or "Clear prior chat matrix" in src


def test_cancel_pending_atomic_rejects_running(memory_db, monkeypatch):
    monkeypatch.setenv("SCHEDULED_DM_ENABLED", "true")
    _add_account(memory_db, 106)
    now = datetime.utcnow()
    job = ScheduledJob(
        account_id=106,
        type=MessageType.DM.value,
        status=JobStatus.RUNNING.value,
        run_at=now + timedelta(minutes=5),
        peer_id="12345",
        peer_type="private",
        message_body="hello",
        created_at=now,
        updated_at=now,
    )
    memory_db.add(job)
    memory_db.commit()
    jid = job.id
    with patch(
        "src.messaging.scheduled_dm_service.scheduled_dm_create_allowed",
        return_value=True,
    ):
        svc = ScheduledDirectMessageService()
        r = svc.cancel(memory_db, job_id=jid)
    assert r.ok is False
    assert r.status_code == 409
    assert r.payload["error_code"] == "CANCEL_NOT_ALLOWED"
    memory_db.refresh(job)
    assert job.status == JobStatus.RUNNING.value


def test_cancel_pending_atomic_succeeds(memory_db, monkeypatch):
    monkeypatch.setenv("SCHEDULED_DM_ENABLED", "true")
    _add_account(memory_db, 106)
    now = datetime.utcnow()
    job = ScheduledJob(
        account_id=106,
        type=MessageType.DM.value,
        status=JobStatus.PENDING.value,
        run_at=now + timedelta(minutes=5),
        peer_id="12345",
        peer_type="private",
        message_body="hello",
        created_at=now,
        updated_at=now,
    )
    memory_db.add(job)
    memory_db.commit()
    jid = job.id
    with patch(
        "src.messaging.scheduled_dm_service.scheduled_dm_create_allowed",
        return_value=True,
    ):
        svc = ScheduledDirectMessageService()
        r = svc.cancel(memory_db, job_id=jid)
    assert r.ok is True
    memory_db.refresh(job)
    assert job.status == JobStatus.CANCELLED.value


def test_join_bulk_skips_waiting_approval(memory_db):
    _add_account(memory_db, 106)
    join_calls = []

    class FakeChat:
        async def preview_async(self, account_id, ref):
            return {
                "ok": True,
                "already_joined": False,
                "message": "Waiting for admin approval",
                "status": "join_requested",
            }

        async def join_async(self, account_id, ref, confirm=False):
            join_calls.append(int(account_id))
            return {"ok": True, "status": "join_requested"}

    orch = MultiAccountJoinOrchestrator(chat_service=FakeChat(), run_async=_run_coro, pause_sec=0)
    with patch(
        "src.messaging.multi_account_join.evaluate_dm_account_eligibility",
        return_value=_elig(),
    ):
        r = orch.join_bulk(
            memory_db, ref="https://t.me/+x", account_ids=[106], confirm=True
        )
    assert join_calls == []
    assert r.payload["waiting_approval"] == 1
    assert r.payload["skipped"] == 1
    assert "not requesting again" in (r.payload["results"][0]["message"] or "").lower()


def test_overdue_skip_fail_closed(memory_db):
    _add_account(memory_db, 106)
    now = datetime.utcnow()
    job = ScheduledJob(
        account_id=106,
        type=MessageType.DM.value,
        status=JobStatus.RUNNING.value,
        run_at=now - timedelta(seconds=MAX_OVERDUE_EXECUTE_SEC + 60),
        peer_id="12345",
        peer_type="private",
        message_body="hello",
        created_at=now,
        updated_at=now,
        lease_owner="worker-test",
        lease_until=now + timedelta(minutes=5),
    )
    memory_db.add(job)
    memory_db.commit()
    jid = job.id
    send_mock = AsyncMock()

    @contextmanager
    def _ctx():
        try:
            yield memory_db
            memory_db.commit()
        except Exception:
            memory_db.rollback()
            raise

    with patch("src.scheduler.executor.get_db_context", _ctx), patch(
        "src.messaging.owner_dm_service.OwnerDirectMessageService.send_now",
        send_mock,
    ):
        ok = _run_coro(_execute_scheduled_dm_job(jid))
    assert ok is False
    send_mock.assert_not_called()
    memory_db.refresh(job)
    assert job.status == JobStatus.FAILED.value
    assert "OVERDUE_SKIPPED" in (job.last_error or "")


def test_deploy_script_invokes_messages_js_gate():
    src = (ROOT / "scripts/release/deploy_production.sh").read_text(encoding="utf-8")
    assert "check_messages_js_syntax.sh" in src
    assert "BROKEN_MESSAGES_JS_DEPLOY_BLOCKED" in src
