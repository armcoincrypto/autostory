"""Dry-run must never publish, mutate auth, or touch production sessions."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.core.database import Base
from src.core.models import Account, AutoStoryCampaign, Story
from src.stories.mutation_boundary import get_provider_call_count, reset_provider_call_counter


@pytest.fixture()
def dry_db(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with engine.connect() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS auto_story_account_locks (
                  account_id INTEGER PRIMARY KEY,
                  campaign_id INTEGER NOT NULL,
                  wave_index INTEGER NOT NULL,
                  locked_at DATETIME,
                  expires_at DATETIME
                )
                """
            )
        )
        conn.commit()
    Session = sessionmaker(bind=engine)
    session = Session()

    @contextmanager
    def _ctx():
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise

    monkeypatch.setattr("src.core.database.get_db_context", _ctx)
    monkeypatch.setattr("src.stories.auto_story_service.get_db_context", _ctx)
    yield session
    session.close()


def test_dry_run_campaign_wave_never_publishes(dry_db, monkeypatch) -> None:
    from src.stories.auto_story_service import dry_run_campaign_wave

    reset_provider_call_counter()
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("STORY_MUTATIONS_ENABLED", "true")
    monkeypatch.setenv("CAMPAIGN_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("CONTROLLED_STORY_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("SCHEDULER_STORY_EXECUTION_ENABLED", "true")

    import os

    assert "opt/autostory/data" not in os.environ.get("DATABASE_URL", "")

    c = AutoStoryCampaign(
        status="active",
        account_ids=[1],
        media_path="/tmp/dry-run.jpg",
        caption="dry-run caption",
        mentions_per_story=0,
        duration_days=1,
        posts_per_day=1,
        times_json=["12:00"],
        stories_per_account_per_day=1,
        started_at=datetime.utcnow(),
        ends_at=datetime.utcnow() + timedelta(days=1),
        explicit_operator_approval=True,
        confirmation_token="I_CONFIRM_STORY_PUBLISH",
    )
    dry_db.add(c)
    dry_db.commit()

    send_calls = {"n": 0}

    def _forbidden_send(*args, **kwargs):
        send_calls["n"] += 1
        raise AssertionError("SendStoryRequest must not run during dry-run")

    monkeypatch.setattr(
        "telethon.tl.functions.stories.SendStoryRequest",
        _forbidden_send,
        raising=False,
    )

    async def _forbidden_invoke(*args, **kwargs):
        raise AssertionError("invoke_send_story must not run during dry-run")

    monkeypatch.setattr(
        "src.stories.mutation_boundary.invoke_send_story",
        _forbidden_invoke,
    )

    def _forbidden_session(*args, **kwargs):
        raise AssertionError("production sessions must not be loaded during dry-run")

    monkeypatch.setattr(
        "src.security.session_material.load_session_material",
        _forbidden_session,
        raising=False,
    )

    monkeypatch.setattr(
        "src.stories.auto_story_service.build_story_dry_run_plan",
        lambda db, payload: {
            "ok": True,
            "dry_run": True,
            "precheck": {"live_publish_allowed": False},
            "selected_mention_candidates": [],
            "per_account_mentions": [],
            "confirmation_token_expected": None,
            "mutation_allowlist_account_ids": [],
        },
    )
    monkeypatch.setattr(
        "src.stories.autostory_hardening.select_next_wave_accounts",
        lambda db, campaign, wave_index=0: {
            "wave_account_ids": [1],
            "blocked": [],
            "unfinished_count": 1,
            "remaining_after_wave": 0,
            "wave_index": int(wave_index),
            "max_wave_size": 25,
            "recurring": False,
        },
    )
    monkeypatch.setattr(
        "src.stories.autostory_hardening.plan_full_fleet_waves",
        lambda db, ids: {
            "wave_count": 1,
            "waves": [{"size": 1}],
            "duplicates": 0,
            "any_over_max": False,
        },
    )

    before_stories = dry_db.query(Story).count()
    out = dry_run_campaign_wave(int(c.id))

    assert out["ok"] is True
    assert out["dry_run"] is True
    assert out["would_publish"] is False
    assert out["reason"] == "dry_run"
    assert out["wave_size"] == 1
    assert out["media"] == "/tmp/dry-run.jpg"
    assert out["caption"] == "dry-run caption"
    assert out["daily_target"] == 1
    assert out["mentions_target"] == 0
    assert get_provider_call_count() == 0
    assert send_calls["n"] == 0
    assert dry_db.query(Story).count() == before_stories
    account = dry_db.get(Account, 1)
    assert account is None  # no auth mutation; no production account row


def test_mutation_boundary_dry_run_issues_no_token(monkeypatch) -> None:
    from src.stories.mutation_boundary import StoryMutationService, StoryMutationTrigger

    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("STORY_MUTATIONS_ENABLED", "true")
    monkeypatch.setenv("STORY_EXECUTION_MODE", "dry-run")
    reset_provider_call_counter()
    decision = StoryMutationService.evaluate(
        account_id=1,
        trigger=StoryMutationTrigger.OPERATOR,
        scope="controlled_live",
        dry_run=True,
        caller="dry_run_isolation",
    )
    assert decision.decision == "dry-run"
    assert decision.authorization is None
    assert decision.provider_called is False
    assert get_provider_call_count() == 0
