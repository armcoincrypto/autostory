"""Focused tests for UTC lazy daily Story counter rollover."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from src.stories.daily_story_counter import (
    ensure_stories_today_current,
    production_story_day,
    record_successful_story_publish,
    stories_today_effective,
)


def _account(**kwargs):
    defaults = {
        "stories_today": 0,
        "stories_today_on": None,
        "last_story_success_at": None,
        "last_active": None,
        "last_action_at": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_same_day_story_remains_counted():
    today = production_story_day()
    acc = _account(stories_today=1, stories_today_on=today)
    assert stories_today_effective(acc) == 1
    assert acc.stories_today == 1
    assert acc.stories_today_on == today


def test_previous_day_story_no_longer_blocks():
    today = production_story_day()
    yesterday = today - timedelta(days=1)
    acc = _account(stories_today=1, stories_today_on=yesterday)
    assert stories_today_effective(acc) == 0
    assert acc.stories_today == 0
    assert acc.stories_today_on == today


def test_timezone_boundary_just_before_and_after_utc_midnight():
    # 2026-07-22 23:59 UTC still that day; 00:00 next day rolls.
    before = datetime(2026, 7, 22, 23, 59, 59, tzinfo=timezone.utc)
    after = datetime(2026, 7, 23, 0, 0, 0, tzinfo=timezone.utc)
    acc = _account(stories_today=1, stories_today_on=date(2026, 7, 22))
    assert ensure_stories_today_current(acc, now=before) == 1
    assert ensure_stories_today_current(acc, now=after) == 0
    assert acc.stories_today_on == date(2026, 7, 23)


def test_restart_safe_persisted_same_day_count():
    today = production_story_day()
    acc = _account(stories_today=1, stories_today_on=today)
    # Simulate process restart: new object, same persisted fields.
    restored = _account(stories_today=acc.stories_today, stories_today_on=acc.stories_today_on)
    assert stories_today_effective(restored) == 1


def test_failed_publish_does_not_increment():
    today = production_story_day()
    acc = _account(stories_today=0, stories_today_on=today)
    # Failures must not call record_successful_story_publish; ensure alone is a no-op.
    assert ensure_stories_today_current(acc) == 0
    assert acc.stories_today == 0


def test_successful_publish_increments_exactly_once():
    today = production_story_day()
    acc = _account(stories_today=0, stories_today_on=today)
    assert record_successful_story_publish(acc) == 1
    assert record_successful_story_publish(acc) == 2
    assert acc.stories_today == 2
    assert acc.stories_today_on == today


def test_idempotent_lazy_rollover():
    yesterday = production_story_day() - timedelta(days=1)
    acc = _account(stories_today=3, stories_today_on=yesterday)
    assert ensure_stories_today_current(acc) == 0
    assert ensure_stories_today_current(acc) == 0
    assert acc.stories_today == 0


def test_concurrent_style_rollover_never_negative():
    yesterday = production_story_day() - timedelta(days=1)
    acc = _account(stories_today=1, stories_today_on=yesterday)
    ensure_stories_today_current(acc)
    record_successful_story_publish(acc)
    assert acc.stories_today == 1
    assert acc.stories_today >= 0


def test_legacy_null_date_keeps_same_day_via_last_success():
    today = production_story_day()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    acc = _account(
        stories_today=1,
        stories_today_on=None,
        last_story_success_at=now,
    )
    assert stories_today_effective(acc) == 1
    assert acc.stories_today_on == today


def test_legacy_null_date_resets_when_last_success_previous_day():
    today = production_story_day()
    yesterday_dt = datetime.combine(today - timedelta(days=1), datetime.min.time())
    acc = _account(
        stories_today=1,
        stories_today_on=None,
        last_story_success_at=yesterday_dt,
    )
    assert stories_today_effective(acc) == 0
    assert acc.stories_today_on == today
