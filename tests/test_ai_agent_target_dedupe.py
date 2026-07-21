"""Target normalization and one-active-task-per-target enforcement."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pytest

_ROOT = Path(__file__).resolve().parents[1]
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.database import Base

import src.core.models  # noqa: F401
import src.core.ai_agent_models  # noqa: F401

from src.ai_agent.auto_loop import AiAgentAutoLoop
from src.ai_agent.public_errors import humanize_ai_agent_error
from src.ai_agent.service import AiAgentService
from src.ai_agent.task_target_dedupe import (
    AI_AGENT_ACTIVE_STATUSES,
    find_active_task_id_for_normalized_target,
    find_newer_active_duplicate,
    normalize_ai_target,
)
from src.core.ai_agent_models import AiAgentAudit, AiAgentTask
from src.core.models import Account, AccountStatus

from tests.test_ai_agent_integration import MockAiClient, MockSender


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


@pytest.fixture
def account_row(db_session):
    a = Account(id=110, phone_number="+19995550999", status=AccountStatus.ACTIVE)
    db_session.add(a)
    db_session.commit()
    db_session.refresh(a)
    return a


@pytest.fixture
def account_row_b(db_session):
    b = Account(id=113, phone_number="+19995550998", status=AccountStatus.ACTIVE)
    db_session.add(b)
    db_session.commit()
    db_session.refresh(b)
    return b


def _task(
    db_session,
    account_id: int,
    *,
    target: str,
    status: str = "draft",
    auto_mode: str = "autonomous",
    created_at: Optional[datetime] = None,
) -> AiAgentTask:
    now = created_at or datetime.utcnow()
    t = AiAgentTask(
        account_id=account_id,
        target_username_or_id=target,
        goal_text="g",
        language="auto",
        tone="professional",
        max_messages=10,
        status=status,
        negotiation_stage="opening",
        auto_mode=auto_mode,
        auto_delay_sec=30,
        last_activity_at=now,
        created_at=now,
        updated_at=now,
    )
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)
    return t


def test_normalize_ai_target_basic():
    assert normalize_ai_target("") == ""
    assert normalize_ai_target("   ") == ""
    assert normalize_ai_target(None) == ""
    assert normalize_ai_target("@ArmSeller") == "@armseller"
    assert normalize_ai_target("ArmSeller") == "@armseller"
    assert normalize_ai_target("  @ArmSeller  ") == "@armseller"
    assert normalize_ai_target("12345") == "12345"
    assert normalize_ai_target("00123") == "123"
    assert normalize_ai_target("@12345") == "12345"


def test_normalize_same_key_case_insensitive():
    assert normalize_ai_target("@Seller") == normalize_ai_target("seller")


def test_find_active_duplicate_same_normalized(db_session, account_row):
    t1 = _task(db_session, account_row.id, target="@DupUser")
    got = find_active_task_id_for_normalized_target(db_session, normalize_ai_target("dupuser"))
    assert got == t1.id


def test_paused_task_does_not_block_active_lookup(db_session, account_row):
    _task(db_session, account_row.id, target="@hold", status="paused")
    assert find_active_task_id_for_normalized_target(db_session, normalize_ai_target("@hold")) is None


def test_completed_task_does_not_block_same_target(db_session, account_row):
    _task(db_session, account_row.id, target="@reopen", status="completed")
    assert find_active_task_id_for_normalized_target(db_session, normalize_ai_target("@reopen")) is None


def test_waiting_reply_blocks_duplicate_lookup(db_session, account_row):
    t = _task(db_session, account_row.id, target="@waiting", status="waiting_reply")
    assert find_active_task_id_for_normalized_target(
        db_session, normalize_ai_target("@waiting")
    ) == int(t.id)


def test_ai_agent_active_statuses_include_core_flow():
    assert "draft" in AI_AGENT_ACTIVE_STATUSES
    assert "waiting_admin_approval" in AI_AGENT_ACTIVE_STATUSES
    assert "paused" not in AI_AGENT_ACTIVE_STATUSES


def test_process_task_supersedes_older_duplicate(
    db_session, account_row, account_row_b
):
    base = datetime(2022, 6, 1, 12, 0, 0)
    t_old = _task(
        db_session,
        account_row.id,
        target="@onepeer",
        created_at=base,
    )
    t_new = _task(
        db_session,
        account_row_b.id,
        target="onepeer",
        created_at=base + timedelta(seconds=5),
    )
    loop = AiAgentAutoLoop(AiAgentService(sender=MockSender(), client=MockAiClient()))
    res = loop.process_task(db_session, t_old.id, ignore_delay=True)
    assert res.get("outcome") == "skipped"
    assert res.get("reason") == "superseded_by_newer_task"
    assert res.get("newer_task_id") == t_new.id
    db_session.refresh(t_old)
    assert t_old.status == "paused"
    aud = (
        db_session.query(AiAgentAudit)
        .filter(
            AiAgentAudit.task_id == t_old.id,
            AiAgentAudit.action == "superseded_by_newer_task",
        )
        .first()
    )
    assert aud is not None


def test_humanize_active_task_exists_for_target():
    msg = humanize_ai_agent_error({"error": "active_task_exists_for_target"})
    assert "active task" in msg.lower()


def test_newer_duplicate_none_when_only_one(db_session, account_row):
    t = _task(db_session, account_row.id, target="@solo")
    assert find_newer_active_duplicate(db_session, t) is None


def test_dedupe_ops_script_help_exits_zero():
    import subprocess
    import sys

    script = _ROOT / "scripts" / "ops" / "ai_agent_dedupe_targets.py"
    if not script.is_file():
        pytest.skip("ops script not present")
    r = subprocess.run(
        [sys.executable, str(script), "-h"],
        cwd=str(_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert r.returncode == 0
    assert "dry" in r.stdout.lower() or "apply" in r.stdout.lower()
