"""AutoStory product retirement freeze (2026-09-03).

Proves the new AUTOSTORY_CAMPAIGN_CREATION_ENABLED gate is fail-closed by
default and blocks every AutoStory campaign entry point (create, activate,
and the operator-manual "Run now" path), while leaving the underlying
feature code itself intact and re-enablable for testing/rollback. See
docs/AUTOSTORY_RETIRED_20260903.md.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.core.database import Base
from src.core.models import Account, AccountStatus, AutoStoryCampaign


@pytest.fixture()
def rec_db(monkeypatch):
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
    monkeypatch.setattr("src.stories.auto_story_service.get_db_context", _ctx, raising=False)
    monkeypatch.setattr("src.stories.autostory_hardening.get_db_context", _ctx, raising=False)
    monkeypatch.delenv("AUTOSTORY_CAMPAIGN_CREATION_ENABLED", raising=False)
    yield session
    session.close()


def _seed_account(db, i: int = 0) -> int:
    a = Account(
        phone_number=f"+1555400{i:04d}",
        status=AccountStatus.ACTIVE,
        stories_today=0,
        stories_today_on=date.today(),
    )
    db.add(a)
    db.commit()
    return int(a.id)


def test_create_campaign_blocked_by_default(rec_db) -> None:
    from src.stories.auto_story_service import create_campaign

    result = create_campaign({"media_path": "/tmp/cert.jpg", "account_ids": [1]})
    assert result["ok"] is False
    assert result["error"] == "autostory_retired"


def test_create_campaign_succeeds_when_explicitly_re_enabled(rec_db, monkeypatch, tmp_path) -> None:
    """The feature is frozen, not deleted -- flipping the flag must still work
    for rollback/testing purposes."""
    from PIL import Image

    from src.stories.auto_story_service import create_campaign

    monkeypatch.setenv("AUTOSTORY_CAMPAIGN_CREATION_ENABLED", "true")
    aid = _seed_account(rec_db)
    media = tmp_path / "story.jpg"
    Image.new("RGB", (1080, 1920), (10, 20, 30)).save(media, format="JPEG")
    result = create_campaign(
        {
            "media_path": str(media),
            "account_ids": [aid],
            "duration_days": 1,
            "posts_per_day": 1,
            "legacy_once": True,
        }
    )
    assert result.get("error") != "autostory_retired"


def test_activate_campaign_blocked_by_default(rec_db) -> None:
    from src.stories.auto_story_service import activate_campaign

    aid = _seed_account(rec_db)
    now = datetime.utcnow()
    c = AutoStoryCampaign(
        status="draft",
        account_ids=[aid],
        media_path="/tmp/cert.jpg",
        caption="cert",
        mentions_per_story=0,
        duration_days=1,
        posts_per_day=1,
        times_json=["10:00"],
        campaign_mode="accounts_publish_once",
        stories_per_account_per_day=1,
        started_at=now,
        ends_at=now + timedelta(days=1),
    )
    rec_db.add(c)
    rec_db.commit()

    result = activate_campaign(int(c.id), {"explicit_operator_approval": True, "preview_reviewed": True})
    assert result["ok"] is False
    assert result["error"] == "autostory_retired"


def test_run_now_manual_path_blocked_by_default(rec_db) -> None:
    """execute_wave(operator_manual=True) -- the underlying call behind the
    "Run now" button -- must refuse even though it deliberately bypasses
    require_scheduler_flag by design for other reasons."""
    from src.stories.auto_story_service import execute_wave

    aid = _seed_account(rec_db)
    now = datetime.utcnow()
    c = AutoStoryCampaign(
        status="active",
        account_ids=[aid],
        media_path="/tmp/cert.jpg",
        caption="cert",
        mentions_per_story=0,
        duration_days=1,
        posts_per_day=1,
        times_json=["10:00"],
        campaign_mode="accounts_publish_once",
        stories_per_account_per_day=1,
        started_at=now,
        ends_at=now + timedelta(days=1),
        next_wave_at=now,
    )
    rec_db.add(c)
    rec_db.commit()

    result = execute_wave(int(c.id), operator_manual=True, require_scheduler_flag=False)
    assert result["ok"] is False
    assert result["error"] == "autostory_retired"
