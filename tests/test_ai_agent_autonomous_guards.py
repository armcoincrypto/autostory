"""Autonomous loop: counterparty-reply and duplicate-outbound guards."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.database import Base
import src.core.models  # noqa: F401
import src.core.ai_agent_models  # noqa: F401

from src.ai_agent.auto_loop import AiAgentAutoLoop
from src.ai_agent.service import (
    AiAgentService,
    autonomous_outbound_is_duplicate_of_recent_sent,
    autonomous_should_wait_for_counterparty_reply,
)
from src.core.ai_agent_models import AiAgentAudit, AiAgentMessage, AiAgentTask
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
    a = Account(id=110, phone_number="+19995550177", status=AccountStatus.ACTIVE)
    db_session.add(a)
    db_session.commit()
    db_session.refresh(a)
    return a


def _mk_task(
    db_session,
    account_id: int,
    *,
    status: str = "waiting_admin_approval",
    auto_mode: str = "autonomous",
) -> AiAgentTask:
    now = datetime.utcnow()
    t = AiAgentTask(
        account_id=account_id,
        target_username_or_id="@peer",
        goal_text="Negotiate",
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


def test_autonomous_should_wait_when_outbound_newer_than_inbound(db_session, account_row):
    t = _mk_task(db_session, account_row.id)
    base = datetime(2024, 6, 1, 12, 0, 0)
    db_session.add(
        AiAgentMessage(
            task_id=t.id,
            direction="in",
            status="stored",
            body="old reply",
            telegram_message_id=1,
            meta_json={},
            created_at=base,
        )
    )
    db_session.add(
        AiAgentMessage(
            task_id=t.id,
            direction="out",
            status="sent",
            body="our send",
            telegram_message_id=10,
            meta_json={},
            created_at=base + timedelta(minutes=5),
        )
    )
    db_session.commit()
    assert autonomous_should_wait_for_counterparty_reply(db_session, t.id) is True


def test_autonomous_continues_when_inbound_newer_than_outbound(db_session, account_row):
    t = _mk_task(db_session, account_row.id)
    base = datetime(2024, 6, 1, 12, 0, 0)
    db_session.add(
        AiAgentMessage(
            task_id=t.id,
            direction="out",
            status="sent",
            body="our send",
            telegram_message_id=10,
            meta_json={},
            created_at=base,
        )
    )
    db_session.add(
        AiAgentMessage(
            task_id=t.id,
            direction="in",
            status="stored",
            body="fresh reply",
            telegram_message_id=2,
            meta_json={},
            created_at=base + timedelta(minutes=5),
        )
    )
    db_session.commit()
    assert autonomous_should_wait_for_counterparty_reply(db_session, t.id) is False


def test_duplicate_draft_matches_recent_sent_normalized(db_session, account_row):
    t = _mk_task(db_session, account_row.id)
    db_session.add(
        AiAgentMessage(
            task_id=t.id,
            direction="out",
            status="sent",
            body="Hello   World",
            telegram_message_id=10,
            meta_json={},
            created_at=datetime.utcnow(),
        )
    )
    db_session.commit()
    assert (
        autonomous_outbound_is_duplicate_of_recent_sent(db_session, t.id, "hello world") is True
    )


def test_auto_loop_skips_duplicate_existing_draft(db_session, account_row):
    sender = MockSender(fetch_messages=[])
    svc = AiAgentService(sender=sender, client=MockAiClient())
    loop = AiAgentAutoLoop(svc)
    t = _mk_task(db_session, account_row.id, status="waiting_admin_approval")
    base = datetime(2024, 6, 1, 12, 0, 0)
    db_session.add(
        AiAgentMessage(
            task_id=t.id,
            direction="in",
            status="stored",
            body="ping",
            telegram_message_id=1,
            meta_json={},
            created_at=base,
        )
    )
    db_session.add(
        AiAgentMessage(
            task_id=t.id,
            direction="out",
            status="sent",
            body="Same text to send again",
            telegram_message_id=100,
            meta_json={},
            created_at=base + timedelta(minutes=1),
        )
    )
    db_session.add(
        AiAgentMessage(
            task_id=t.id,
            direction="in",
            status="stored",
            body="pong",
            telegram_message_id=2,
            meta_json={},
            created_at=base + timedelta(minutes=2),
        )
    )
    db_session.add(
        AiAgentMessage(
            task_id=t.id,
            direction="out",
            status="draft",
            body="same  text\n\nto send again",
            telegram_message_id=None,
            meta_json={},
            created_at=base + timedelta(minutes=3),
        )
    )
    db_session.commit()
    res = loop.process_task(db_session, t.id, ignore_delay=False)
    db_session.commit()
    assert res.get("reason") == "duplicate_outbound_skipped"
    assert sender.send_calls == []
    aud = [a.action for a in db_session.query(AiAgentAudit).filter(AiAgentAudit.task_id == t.id).all()]
    assert "duplicate_outbound_skipped" in aud


def test_auto_loop_skips_waiting_for_counterparty_on_existing_draft(db_session, account_row):
    sender = MockSender(fetch_messages=[])
    svc = AiAgentService(sender=sender, client=MockAiClient())
    loop = AiAgentAutoLoop(svc)
    t = _mk_task(db_session, account_row.id, status="waiting_admin_approval")
    base = datetime(2024, 6, 1, 12, 0, 0)
    db_session.add(
        AiAgentMessage(
            task_id=t.id,
            direction="in",
            status="stored",
            body="peer",
            telegram_message_id=1,
            meta_json={},
            created_at=base,
        )
    )
    db_session.add(
        AiAgentMessage(
            task_id=t.id,
            direction="out",
            status="sent",
            body="we replied",
            telegram_message_id=50,
            meta_json={},
            created_at=base + timedelta(minutes=1),
        )
    )
    db_session.add(
        AiAgentMessage(
            task_id=t.id,
            direction="out",
            status="draft",
            body="unique follow-up draft",
            telegram_message_id=None,
            meta_json={},
            created_at=base + timedelta(minutes=2),
        )
    )
    db_session.commit()
    res = loop.process_task(db_session, t.id, ignore_delay=False)
    db_session.commit()
    assert res.get("reason") == "waiting_for_counterparty_reply"
    assert sender.send_calls == []
    aud = [a.action for a in db_session.query(AiAgentAudit).filter(AiAgentAudit.task_id == t.id).all()]
    assert "waiting_for_counterparty_reply" in aud


def test_auto_loop_run_now_does_not_bypass_account_governance(db_session, account_row):
    sender = MockSender(fetch_messages=[])
    svc = AiAgentService(sender=sender, client=MockAiClient())
    loop = AiAgentAutoLoop(svc)
    t = _mk_task(db_session, account_row.id, status="waiting_admin_approval")
    base = datetime(2024, 6, 1, 12, 0, 0)
    db_session.add(
        AiAgentMessage(
            task_id=t.id,
            direction="out",
            status="sent",
            body="dup body",
            telegram_message_id=100,
            meta_json={},
            created_at=base,
        )
    )
    db_session.add(
        AiAgentMessage(
            task_id=t.id,
            direction="out",
            status="draft",
            body="dup body",
            telegram_message_id=None,
            meta_json={},
            created_at=base + timedelta(minutes=1),
        )
    )
    db_session.commit()
    res = loop.process_task(db_session, t.id, ignore_delay=True)
    db_session.commit()
    assert res.get("sent") is False
    assert res.get("error_code") == "account_governance_block"
    assert sender.send_calls == []


def test_auto_loop_cold_start_respects_account_governance(db_session, account_row):
    sender = MockSender()
    svc = AiAgentService(sender=sender, client=MockAiClient())
    loop = AiAgentAutoLoop(svc)
    t = _mk_task(db_session, account_row.id, status="draft")
    db_session.commit()
    res = loop.process_task(db_session, t.id, ignore_delay=True)
    db_session.commit()
    assert res.get("phase") == "cold_start"
    assert res.get("sent") is False
    assert res.get("error_code") == "account_governance_block"
    assert sender.send_calls == []


def test_auto_loop_skips_non_ai_reserved_account(db_session):
    a = Account(id=107, phone_number="+19995550107", status=AccountStatus.ACTIVE)
    db_session.add(a)
    db_session.commit()
    db_session.refresh(a)
    loop = AiAgentAutoLoop(AiAgentService(sender=MockSender(), client=MockAiClient()))
    t = _mk_task(db_session, a.id)
    db_session.commit()
    res = loop.process_task(db_session, t.id, ignore_delay=True)
    assert res.get("outcome") == "skipped"
    assert res.get("reason") == "account_not_reserved_for_ai_agent"
