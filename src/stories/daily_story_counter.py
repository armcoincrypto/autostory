"""UTC calendar-day Story success counter (lazy rollover).

``Account.stories_today`` is a persisted success counter. Celery
``reset_daily_counters`` was the historical reset path, but beat is not
deployed. This module provides the production day-boundary behavior:

* Timezone: UTC calendar day (matches Celery ``timezone=UTC`` and
  ``utc_day_bounds_naive``).
* Lazy rollover: when ``stories_today_on`` is missing or older than today,
  the counter resets to 0 before any cap check or increment.
* Same-day publishes remain counted; process restarts do not bypass the
  persisted counter.
* Failed publishes must not call ``record_successful_story_publish``.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Optional

from src.core.datetime_utc import utc_day_bounds_naive


def production_story_day(now: Optional[datetime] = None) -> date:
    """UTC calendar date used for Story daily limits."""
    start, _ = utc_day_bounds_naive(now)
    return start.date()


def _as_utc_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).date()
        return value.date()
    if isinstance(value, str) and value.strip():
        raw = value.strip().replace("Z", "+00:00")
        try:
            return _as_utc_date(datetime.fromisoformat(raw))
        except ValueError:
            try:
                return date.fromisoformat(raw[:10])
            except ValueError:
                return None
    return None


def _legacy_count_belongs_to_today(account: Any, today: date) -> bool:
    """When ``stories_today_on`` is unset, keep a non-zero count only if last activity is today."""
    for attr in ("last_story_success_at", "last_active", "last_action_at"):
        day = _as_utc_date(getattr(account, attr, None))
        if day is not None:
            return day == today
    return False


def ensure_stories_today_current(account: Any, *, now: Optional[datetime] = None) -> int:
    """
    Ensure ``stories_today`` reflects the current UTC production day in-memory.

    Returns the effective count after any lazy rollover. Caller persists when
    operating inside a DB session that will commit.
    """
    today = production_story_day(now)
    stored_on = _as_utc_date(getattr(account, "stories_today_on", None))
    current = int(getattr(account, "stories_today", None) or 0)

    if stored_on == today:
        return current

    if stored_on is None and current > 0 and _legacy_count_belongs_to_today(account, today):
        account.stories_today_on = today
        return current

    account.stories_today = 0
    account.stories_today_on = today
    return 0


def record_successful_story_publish(account: Any, *, now: Optional[datetime] = None) -> int:
    """
    Increment the success counter exactly once for a confirmed publish.

    Applies lazy day rollover first so a previous-day residue cannot inflate
    today's count.
    """
    ensure_stories_today_current(account, now=now)
    today = production_story_day(now)
    next_count = int(getattr(account, "stories_today", None) or 0) + 1
    if next_count < 1:
        next_count = 1
    account.stories_today = next_count
    account.stories_today_on = today
    return next_count


def stories_today_effective(account: Any, *, now: Optional[datetime] = None) -> int:
    """Read-side helper: rollover then return the effective count."""
    return ensure_stories_today_current(account, now=now)
