"""Wave F: Active Campaign Card honesty -- Day X/Y and real shortfall reasons.

No Telegram calls. Exercises the pure day-number helper, the durable-log
shortfall reason aggregator, and _campaign_progress's finalized-vs-in-progress
distinction directly against an in-memory DB.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.database import Base
from src.core.models import (
    Account,
    AccountStatus,
    AutoStoryAccountProgress,
    AutoStoryCampaign,
    AutoStoryDailyProgress,
)
from src.stories.auto_story_service import _campaign_progress, campaign_to_dict
from src.stories.autostory_recurring import (
    campaign_current_day_number,
    campaign_daily_progress_summary,
    campaign_shortfall_reason,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _account(db, account_id: int) -> Account:
    a = Account(id=account_id, phone_number=f"+1555400{account_id:04d}", status=AccountStatus.ACTIVE)
    db.add(a)
    db.commit()
    return a


def _campaign(db, *, campaign_id: int | None = None, status: str, started_at, duration_days: int, account_ids: list[int]) -> AutoStoryCampaign:
    c = AutoStoryCampaign(
        id=campaign_id,
        status=status,
        account_ids=account_ids,
        media_path="data/media/x.jpg",
        caption="",
        mentions_per_story=0,
        duration_days=duration_days,
        posts_per_day=1,
        times_json=["10:00"],
        campaign_mode="recurring_daily",
        stories_per_account_per_day=1,
        started_at=started_at,
        ends_at=started_at + timedelta(days=duration_days),
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _daily_row(db, *, campaign_id, account_id, local_date, target, success, failed, completed):
    db.add(
        AutoStoryDailyProgress(
            campaign_id=campaign_id,
            account_id=account_id,
            local_date=local_date,
            target_count=target,
            successful_count=success,
            failed_count=failed,
            ambiguous_count=0,
            completed_for_day=completed,
        )
    )


def _progress_row(db, *, campaign_id, wave_index, account_id, status, error=None):
    db.add(
        AutoStoryAccountProgress(
            campaign_id=campaign_id,
            wave_index=wave_index,
            account_id=account_id,
            status=status,
            error=error,
        )
    )


# ---------------------------------------------------------------------------
# Day X/Y
# ---------------------------------------------------------------------------


def test_day_number_before_start_is_day_one() -> None:
    tomorrow = datetime.now(tz=None) + timedelta(days=5)
    n = campaign_current_day_number(started_at=tomorrow, duration_days=7, tz_name="UTC")
    assert n == 1


def test_day_number_on_first_local_date_is_one() -> None:
    now = datetime.utcnow()
    n = campaign_current_day_number(started_at=now, duration_days=5, tz_name="UTC")
    assert n == 1


def test_day_number_mid_campaign() -> None:
    started = datetime.utcnow() - timedelta(days=2)
    n = campaign_current_day_number(started_at=started, duration_days=5, tz_name="UTC")
    assert n == 3


def test_day_number_clamped_at_final_day_past_end() -> None:
    started = datetime.utcnow() - timedelta(days=30)
    n = campaign_current_day_number(started_at=started, duration_days=5, tz_name="UTC")
    assert n == 5


# ---------------------------------------------------------------------------
# Shortfall reason aggregation (durable log, no new data collection)
# ---------------------------------------------------------------------------


def test_shortfall_reason_none_when_nothing_failed(db) -> None:
    assert campaign_shortfall_reason(db, 999) is None


def test_shortfall_reason_ignores_successful_rows(db) -> None:
    _account(db, 1)
    _progress_row(db, campaign_id=1, wave_index=0, account_id=1, status="reconciled", error=None)
    db.commit()
    assert campaign_shortfall_reason(db, 1) is None


def test_shortfall_reason_aggregates_dominant_cause(db) -> None:
    for i, (status, err) in enumerate(
        [
            ("failed", "story_precheck_failed"),
            ("failed", "story_precheck_failed"),
            ("deferred", "fresh_story_auth_required"),
        ]
    ):
        _account(db, 100 + i)
        _progress_row(db, campaign_id=5, wave_index=0, account_id=100 + i, status=status, error=err)
    db.commit()
    reason = campaign_shortfall_reason(db, 5)
    assert reason is not None
    assert "story_precheck_failed (2)" in reason
    assert "fresh_story_auth_required (1)" in reason
    assert reason.startswith("3 not published:")


def test_shortfall_reason_caps_and_buckets_remainder(db) -> None:
    reasons = ["a", "a", "b", "c", "d", "e"]
    for i, r in enumerate(reasons):
        _account(db, 200 + i)
        _progress_row(db, campaign_id=6, wave_index=0, account_id=200 + i, status="failed", error=r)
    db.commit()
    reason = campaign_shortfall_reason(db, 6, max_reasons=2)
    assert reason is not None
    assert "a (2)" in reason
    assert "other (3)" in reason


def test_shortfall_reason_scoped_to_its_own_campaign(db) -> None:
    _account(db, 301)
    _progress_row(db, campaign_id=7, wave_index=0, account_id=301, status="failed", error="x")
    db.commit()
    assert campaign_shortfall_reason(db, 8) is None


# ---------------------------------------------------------------------------
# campaign_daily_progress_summary: finalized vs in-progress shortfall
# ---------------------------------------------------------------------------


def test_finalized_shortfall_ignores_in_progress_day(db) -> None:
    today = date.today()
    _daily_row(db, campaign_id=1, account_id=1, local_date=today, target=1, success=0, failed=0, completed=False)
    db.commit()
    summary = campaign_daily_progress_summary(db, 1)
    assert summary["finalized_shortfall"] == 0


def test_finalized_shortfall_counts_completed_day_gap(db) -> None:
    yesterday = date.today() - timedelta(days=1)
    for aid in (1, 2, 3):
        succeeded = aid == 1
        _daily_row(
            db, campaign_id=2, account_id=aid, local_date=yesterday, target=1,
            success=1 if succeeded else 0, failed=0 if succeeded else 1, completed=True,
        )
    db.commit()
    summary = campaign_daily_progress_summary(db, 2)
    assert summary["finalized_shortfall"] == 2
    assert summary["successful_count"] == 1
    assert summary["target_count"] == 3


def test_finalized_shortfall_combines_multiple_finalized_days(db) -> None:
    day1 = date.today() - timedelta(days=2)
    day2 = date.today() - timedelta(days=1)
    _daily_row(db, campaign_id=3, account_id=1, local_date=day1, target=1, success=0, failed=1, completed=True)
    _daily_row(db, campaign_id=3, account_id=1, local_date=day2, target=1, success=0, failed=1, completed=True)
    db.commit()
    summary = campaign_daily_progress_summary(db, 3)
    assert summary["finalized_shortfall"] == 2


# ---------------------------------------------------------------------------
# _campaign_progress: end-to-end wiring (the actual API/UI data contract)
# ---------------------------------------------------------------------------


def test_active_campaign_with_finalized_shortfall_surfaces_it(db) -> None:
    started = datetime.utcnow() - timedelta(days=1, hours=1)
    account_ids = [10, 11, 12]
    for aid in account_ids:
        _account(db, aid)
    c = _campaign(db, status="active", started_at=started, duration_days=3, account_ids=account_ids)

    yesterday = (started).date()
    for i, aid in enumerate(account_ids):
        succeeded = i == 0
        _daily_row(
            db, campaign_id=c.id, account_id=aid, local_date=yesterday, target=1,
            success=1 if succeeded else 0, failed=0 if succeeded else 1, completed=True,
        )
        if not succeeded:
            _progress_row(db, campaign_id=c.id, wave_index=0, account_id=aid, status="failed", error="story_precheck_failed")
    db.commit()

    d = campaign_to_dict(c)
    prog = _campaign_progress(db, c, d["max_story_publishes"])
    assert prog["shortfall"] == 2
    assert prog["shortfall_reason"] == "2 not published: story_precheck_failed (2)"
    assert prog["day_number"] == 2
    assert prog["total_days"] == 3


def test_active_campaign_with_only_in_progress_day_has_no_shortfall(db) -> None:
    started = datetime.utcnow() - timedelta(hours=1)
    account_ids = [20, 21]
    for aid in account_ids:
        _account(db, aid)
    c = _campaign(db, status="active", started_at=started, duration_days=3, account_ids=account_ids)

    today = started.date()
    for aid in account_ids:
        _daily_row(db, campaign_id=c.id, account_id=aid, local_date=today, target=1, success=0, failed=0, completed=False)
    db.commit()

    d = campaign_to_dict(c)
    prog = _campaign_progress(db, c, d["max_story_publishes"])
    assert "shortfall" not in prog
    assert "shortfall_reason" not in prog


def test_completed_campaign_still_reports_shortfall(db) -> None:
    started = datetime.utcnow() - timedelta(days=5)
    account_ids = [30]
    _account(db, 30)
    c = _campaign(db, status="completed", started_at=started, duration_days=1, account_ids=account_ids)
    _daily_row(db, campaign_id=c.id, account_id=30, local_date=started.date(), target=1, success=0, failed=1, completed=True)
    _progress_row(db, campaign_id=c.id, wave_index=0, account_id=30, status="failed", error="daily_story_cap")
    db.commit()

    d = campaign_to_dict(c)
    prog = _campaign_progress(db, c, d["max_story_publishes"])
    assert prog["shortfall"] == 1
    assert prog["shortfall_reason"] == "1 not published: daily_story_cap (1)"


def test_fully_successful_campaign_has_no_shortfall_key(db) -> None:
    started = datetime.utcnow() - timedelta(days=2)
    account_ids = [40, 41]
    for aid in account_ids:
        _account(db, aid)
    c = _campaign(db, status="completed", started_at=started, duration_days=1, account_ids=account_ids)
    for aid in account_ids:
        _daily_row(db, campaign_id=c.id, account_id=aid, local_date=started.date(), target=1, success=1, failed=0, completed=True)
    db.commit()

    d = campaign_to_dict(c)
    prog = _campaign_progress(db, c, d["max_story_publishes"])
    assert "shortfall" not in prog
    assert prog["successful_count"] == prog["target_count"]
