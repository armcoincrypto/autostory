"""AI Agent dedicated account allowlist (env-driven)."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config.settings import settings
from src.ai_agent.account_allowlist import (
    account_id_is_ai_agent_allowed,
    ai_agent_allowlist_configured,
    normalize_phone_for_match,
    resolve_ai_agent_allowed_account_ids,
    resolve_ai_agent_task_permitted_account_ids,
)
from src.core.database import Base
from src.core.models import Account, AccountStatus

import src.core.models  # noqa: F401


@pytest.fixture
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    for pid, phone in [
        (110, "+18093262888"),
        (113, "+18093262928"),
        (999, "+19995550123"),
    ]:
        s.add(Account(id=pid, phone_number=phone, status=AccountStatus.ACTIVE))
    s.commit()
    yield s
    s.close()


def test_normalize_phone_for_match():
    assert normalize_phone_for_match("18093262888") == "+18093262888"
    assert normalize_phone_for_match("+1 809 326 2888") == "+18093262888"


def test_resolve_allowlist_phones_only(memory_db, monkeypatch):
    monkeypatch.setattr(settings, "ai_agent_account_phones", "+18093262888,+18093262928")
    monkeypatch.setattr(settings, "ai_agent_account_ids", "")
    r = resolve_ai_agent_allowed_account_ids(memory_db)
    assert r == frozenset({110, 113})


def test_resolve_allowlist_ids_only(memory_db, monkeypatch):
    monkeypatch.setattr(settings, "ai_agent_account_phones", "")
    monkeypatch.setattr(settings, "ai_agent_account_ids", "999,110")
    r = resolve_ai_agent_allowed_account_ids(memory_db)
    assert r == frozenset({999, 110})


def test_account_not_allowed_when_configured(memory_db, monkeypatch):
    monkeypatch.setattr(settings, "ai_agent_account_phones", "+18093262888")
    monkeypatch.setattr(settings, "ai_agent_account_ids", "")
    assert account_id_is_ai_agent_allowed(memory_db, 113) is False
    assert account_id_is_ai_agent_allowed(memory_db, 110) is True


def test_readiness_store_ai_agent_purpose_not_campaign_ready():
    from src.clients.readiness_store import compute_campaign_ready_fields

    out = compute_campaign_ready_fields(
        account_status="active",
        purpose="ai_agent",
        readiness_status="READY",
        readiness_state="READY",
    )
    assert out["campaign_relevant"] is False
    assert out["campaign_ready"] is False
    assert "AI Agent" in (out.get("campaign_ready_label") or "")


def test_task_permitted_includes_ai_agent_purpose(memory_db, monkeypatch):
    monkeypatch.setattr(settings, "ai_agent_account_phones", "")
    monkeypatch.setattr(settings, "ai_agent_account_ids", "")
    memory_db.add(
        Account(
            id=2001,
            phone_number="+19995552001",
            status=AccountStatus.ACTIVE,
            purpose="ai_agent",
        )
    )
    memory_db.commit()
    r = resolve_ai_agent_task_permitted_account_ids(memory_db)
    assert 2001 in r


def test_task_permitted_union_includes_reserved_when_allowlist_empty(memory_db, monkeypatch):
    monkeypatch.setattr(settings, "ai_agent_account_phones", "")
    monkeypatch.setattr(settings, "ai_agent_account_ids", "")
    assert ai_agent_allowlist_configured() is False
    assert account_id_is_ai_agent_allowed(memory_db, 999) is True
    r = resolve_ai_agent_task_permitted_account_ids(memory_db)
    assert 110 in r and 113 in r and 131 in r
    assert 999 not in r


def test_telegram_single_sender_blocks_non_allowlisted_send(memory_db, monkeypatch):
    monkeypatch.setattr(settings, "ai_agent_account_phones", "+18093262888")
    monkeypatch.setattr(settings, "ai_agent_account_ids", "")
    monkeypatch.setenv("AI_AGENT_USE_TELEGRAM_GATEWAY", "false")
    from contextlib import contextmanager

    from sqlalchemy.orm import sessionmaker as sm

    engine = memory_db.bind
    TestSession = sm(bind=engine, expire_on_commit=False)

    @contextmanager
    def get_ctx():
        s = TestSession()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    monkeypatch.setattr("src.ai_agent.telegram_single_sender.get_db_context", get_ctx)
    monkeypatch.setattr(
        "src.core.execution_guard.require_execution_allowed",
        lambda *args, **kwargs: None,
    )

    from src.ai_agent.telegram_single_sender import TelegramSingleSender

    out = TelegramSingleSender().send_message(999, "@x", "hi")
    assert out.get("ok") is False
    assert out.get("error_code") == "forbidden_account"
