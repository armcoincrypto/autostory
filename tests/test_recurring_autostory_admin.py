"""Recurring AutoStory + admin simplification contracts (no live Stories)."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.core.database import Base
from src.core.models import (
    Account,
    AutoStoryAccountProgress,
    AutoStoryCampaign,
    AutoStoryDailyProgress,
)
from src.stories.autostory_recurring import (
    CAMPAIGN_MODE_ONCE,
    CAMPAIGN_MODE_RECURRING,
    MAX_STORIES_PER_ACCOUNT_PER_DAY,
    campaign_mode_of,
    clamp_stories_per_account_per_day,
    ensure_daily_progress_rows,
    is_recurring,
    max_story_publishes,
    record_daily_ambiguous,
    record_daily_success,
    remaining_today,
    select_next_recurring_wave_accounts,
)


def test_max_story_publishes_formula():
    assert max_story_publishes(5, 1, 3) == 15
    assert max_story_publishes(25, 1, 7) == 175
    # Helper remains ready when platform cap is raised later
    assert max_story_publishes(25, 2, 7) == 350


def test_spad_clamped_to_platform_cap(monkeypatch):
    monkeypatch.setenv("MAX_STORIES_PER_ACCOUNT_PER_DAY", "1")
    # Re-import clamp uses module constant; assert constant is 1 in this release
    assert MAX_STORIES_PER_ACCOUNT_PER_DAY == 1
    assert clamp_stories_per_account_per_day(1) == 1
    assert clamp_stories_per_account_per_day(3) == 1
    assert clamp_stories_per_account_per_day(0) == 1


def test_legacy_null_mode_is_once():
    assert campaign_mode_of({"campaign_mode": None}) == CAMPAIGN_MODE_ONCE
    assert campaign_mode_of({}) == CAMPAIGN_MODE_ONCE
    assert is_recurring({"campaign_mode": None}) is False
    assert is_recurring({"campaign_mode": CAMPAIGN_MODE_RECURRING}) is True


@pytest.fixture()
def recur_db(monkeypatch):
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
    yield session
    session.close()


def _seed_accounts(db, n: int) -> list[int]:
    from datetime import date as date_cls
    from src.core.models import AccountStatus

    ids = []
    for i in range(n):
        a = Account(
            phone_number=f"+1555000{i:04d}",
            status=AccountStatus.ACTIVE,
            stories_today=0,
            stories_today_on=date_cls.today(),
        )
        db.add(a)
        db.flush()
        ids.append(int(a.id))
    db.commit()
    return ids


def test_daily_success_blocks_second_when_spad_1(recur_db, monkeypatch):
    db = recur_db
    ids = _seed_accounts(db, 2)
    c = AutoStoryCampaign(
        status="active",
        account_ids=ids,
        media_path="/tmp/x.jpg",
        caption="t",
        mentions_per_story=0,
        duration_days=2,
        posts_per_day=1,
        times_json=["06:00"],
        campaign_mode=CAMPAIGN_MODE_RECURRING,
        stories_per_account_per_day=1,
        started_at=datetime.utcnow(),
        ends_at=datetime.utcnow() + timedelta(days=2),
    )
    db.add(c)
    db.commit()
    from src.stories.autostory_recurring import campaign_local_dates, campaign_local_today

    dates = campaign_local_dates(started_at=c.started_at, duration_days=2)
    ensure_daily_progress_rows(
        db, campaign_id=int(c.id), account_ids=ids, local_dates=dates, target_count=1
    )
    today = campaign_local_today()
    r1 = record_daily_success(db, campaign_id=int(c.id), account_id=ids[0], local_date=today)
    assert r1["ok"] is True
    assert remaining_today(db, campaign_id=int(c.id), account_id=ids[0], local_date=today) == 0
    r2 = record_daily_success(db, campaign_id=int(c.id), account_id=ids[0], local_date=today)
    assert r2["ok"] is False
    assert r2.get("error") == "daily_target_already_met"


def test_ambiguous_blocks_retry(recur_db):
    db = recur_db
    ids = _seed_accounts(db, 1)
    c = AutoStoryCampaign(
        status="active",
        account_ids=ids,
        media_path="/tmp/x.jpg",
        caption="t",
        mentions_per_story=0,
        duration_days=1,
        posts_per_day=1,
        times_json=["06:00"],
        campaign_mode=CAMPAIGN_MODE_RECURRING,
        stories_per_account_per_day=1,
        started_at=datetime.utcnow(),
        ends_at=datetime.utcnow() + timedelta(days=1),
    )
    db.add(c)
    db.commit()
    from src.stories.autostory_recurring import campaign_local_dates, campaign_local_today

    dates = campaign_local_dates(started_at=c.started_at, duration_days=1)
    ensure_daily_progress_rows(
        db, campaign_id=int(c.id), account_ids=ids, local_dates=dates, target_count=1
    )
    today = campaign_local_today()
    record_daily_ambiguous(db, campaign_id=int(c.id), account_id=ids[0], local_date=today)
    assert remaining_today(db, campaign_id=int(c.id), account_id=ids[0], local_date=today) == 0
    r = record_daily_success(db, campaign_id=int(c.id), account_id=ids[0], local_date=today)
    assert r["ok"] is False
    assert r.get("error") == "ambiguous_blocks_retry"


def test_wave_cap_never_exceeds_25(recur_db, monkeypatch):
    db = recur_db
    ids = _seed_accounts(db, 60)
    c = AutoStoryCampaign(
        status="active",
        account_ids=ids,
        media_path="/tmp/x.jpg",
        caption="t",
        mentions_per_story=0,
        duration_days=1,
        posts_per_day=1,
        times_json=["06:00"],
        campaign_mode=CAMPAIGN_MODE_RECURRING,
        stories_per_account_per_day=1,
        started_at=datetime.utcnow(),
        ends_at=datetime.utcnow() + timedelta(days=1),
    )
    db.add(c)
    db.commit()

    monkeypatch.setattr(
        "src.stories.autostory_hardening.is_account_certified_publish",
        lambda db, aid: (True, None),
    )
    monkeypatch.setattr(
        "src.stories.autostory_hardening.account_has_daily_capacity",
        lambda db, aid: (True, None),
    )
    monkeypatch.setattr(
        "src.stories.autostory_hardening.rotate_account_ids",
        lambda db, ids: list(ids),
    )
    sel = select_next_recurring_wave_accounts(db, c, wave_index=0)
    assert len(sel["wave_account_ids"]) <= 25
    assert sel["max_wave_size"] == 25


def test_legacy_once_select_path_unchanged(recur_db, monkeypatch):
    from src.stories.autostory_hardening import select_next_wave_accounts, PROGRESS_NO_RESEND

    db = recur_db
    ids = _seed_accounts(db, 3)
    c = AutoStoryCampaign(
        status="active",
        account_ids=ids,
        media_path="/tmp/x.jpg",
        caption="t",
        mentions_per_story=0,
        duration_days=3,
        posts_per_day=1,
        times_json=["06:00"],
        campaign_mode=CAMPAIGN_MODE_ONCE,
        stories_per_account_per_day=1,
        started_at=datetime.utcnow(),
        ends_at=datetime.utcnow() + timedelta(days=3),
    )
    db.add(c)
    db.commit()
    # Mark first account terminal for whole campaign
    db.add(
        AutoStoryAccountProgress(
            campaign_id=int(c.id),
            wave_index=0,
            account_id=ids[0],
            status="reconciled",
        )
    )
    db.commit()
    monkeypatch.setattr(
        "src.stories.autostory_hardening.is_account_certified_publish",
        lambda db, aid: (True, None),
    )
    monkeypatch.setattr(
        "src.stories.autostory_hardening.account_has_daily_capacity",
        lambda db, aid: (True, None),
    )
    monkeypatch.setattr(
        "src.stories.autostory_hardening.rotate_account_ids",
        lambda db, ids: list(ids),
    )
    sel = select_next_wave_accounts(db, c, wave_index=1)
    assert ids[0] not in sel["wave_account_ids"]
    assert "recurring" not in sel or not sel.get("recurring")
    # daily table unused for legacy
    assert db.query(AutoStoryDailyProgress).count() == 0


def test_recurring_next_day_ignores_once_mode_progress_no_resend(recur_db, monkeypatch):
    """recurring_daily eligibility is daily-progress scoped, not PROGRESS_NO_RESEND."""
    from datetime import date as date_cls

    from src.stories.autostory_hardening import PROGRESS_NO_RESEND

    db = recur_db
    ids = _seed_accounts(db, 1)
    c = AutoStoryCampaign(
        status="active",
        account_ids=ids,
        media_path="/tmp/x.jpg",
        caption="t",
        mentions_per_story=0,
        duration_days=2,
        posts_per_day=1,
        times_json=["06:00"],
        campaign_mode=CAMPAIGN_MODE_RECURRING,
        stories_per_account_per_day=1,
        started_at=datetime.utcnow(),
        ends_at=datetime.utcnow() + timedelta(days=2),
    )
    db.add(c)
    db.commit()
    day0 = date_cls(2026, 8, 20)
    day1 = date_cls(2026, 8, 21)
    ensure_daily_progress_rows(
        db,
        campaign_id=int(c.id),
        account_ids=ids,
        local_dates=[day0, day1],
        target_count=1,
    )
    db.add(
        AutoStoryAccountProgress(
            campaign_id=int(c.id),
            wave_index=0,
            account_id=ids[0],
            status="reconciled",
        )
    )
    db.commit()
    assert "reconciled" in PROGRESS_NO_RESEND
    r1 = record_daily_success(
        db, campaign_id=int(c.id), account_id=ids[0], local_date=day0
    )
    assert r1["ok"] is True
    assert remaining_today(db, campaign_id=int(c.id), account_id=ids[0], local_date=day0) == 0
    assert remaining_today(db, campaign_id=int(c.id), account_id=ids[0], local_date=day1) == 1

    monkeypatch.setattr(
        "src.stories.autostory_recurring.campaign_local_today",
        lambda **kwargs: day1,
    )
    monkeypatch.setattr(
        "src.stories.autostory_hardening.is_account_certified_publish",
        lambda db, aid: (True, None),
    )
    monkeypatch.setattr(
        "src.stories.autostory_hardening.account_has_daily_capacity",
        lambda db, aid: (True, None),
    )
    monkeypatch.setattr(
        "src.stories.autostory_hardening.rotate_account_ids",
        lambda db, ids: list(ids),
    )
    sel = select_next_recurring_wave_accounts(db, c, wave_index=1)
    assert ids[0] in sel["wave_account_ids"]
    assert sel["recurring"] is True
    assert sel["local_date"] == day1.isoformat()


def test_preview_recurring_max_formula(monkeypatch):
    from src.stories.autostory_operator_preview import build_operator_campaign_preview

    monkeypatch.setenv("AUTOSTORY_MENTIONS_PRODUCTION_CERTIFIED", "false")
    monkeypatch.setattr(
        "src.stories.autostory_media.validate_campaign_media",
        lambda path: {
            "ok": True,
            "path": path,
            "error": None,
            "needs_preparation": False,
            "ready_for_story": True,
        },
    )
    db = MagicMock()
    # Minimal automatic selection bypass — pass explicit accounts
    preview = build_operator_campaign_preview(
        db,
        {
            "account_ids": [1, 2, 3, 4, 5],
            "selection_mode": "manual",
            "media_path": "/tmp/ready.jpg",
            "caption": "hi",
            "mentions_per_story": 0,
            "campaign_mode": CAMPAIGN_MODE_RECURRING,
            "stories_per_account_per_day": 1,
            "duration_days": 3,
            "awake_start_hhmm": "10:00",
            "awake_end_hhmm": "20:00",
        },
    )
    assert preview.get("ok") is True
    assert preview.get("max_story_publishes") == 15
    assert preview.get("campaign_mode") == CAMPAIGN_MODE_RECURRING
    assert int(preview.get("mentions_per_story") or 0) == 0
    assert preview.get("mentions_off") is True


def test_ui_primary_has_schedule_cta_no_pickup_slots_day():
    html = Path("src/dashboard/templates/stories.html").read_text(encoding="utf-8")
    # Primary CTA
    assert "Schedule AutoStory" in html
    # Must not expose old primary label
    assert "Pickup slots / day" not in html
    assert "Pickup slots/day" not in html
    # Primary approve dual CTA removed
    assert "Approve &amp; Schedule" not in html
    assert "Approve &amp; Run" not in html
    assert 'id="btn-auto-approve"' in html
    assert "Stories per account per day" in html
