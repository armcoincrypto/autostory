"""
UTC wall-clock helpers for API + DB comparisons.

Contract (scheduler / deliveries):
- ORM ``DateTime`` columns are **naive** and represent **UTC**
  (written via ``utc_now_naive()`` / ``datetime.now(timezone.utc).replace(tzinfo=None)``).
- JSON must expose timestamps as ISO-8601 **with a trailing ``Z``** so browsers parse as UTC.
- Owner local wall-clock conversion lives in ``src.scheduler.timezone`` (Wave 9).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional, Tuple


def utc_now_naive() -> datetime:
    """Current UTC instant as naive datetime (canonical DB/worker clock)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_day_bounds_naive(now: Optional[datetime] = None) -> Tuple[datetime, datetime]:
    """
    Half-open UTC calendar-day range ``[start, end)`` as **naive UTC** datetimes.

    Matches how ``MessageDelivery.created_at`` and ``sent_at`` are stored compared in SQLite/Postgres
    when values are naive UTC instants.
    """
    if now is None:
        ref = datetime.now(timezone.utc)
    elif getattr(now, "tzinfo", None):
        ref = now.astimezone(timezone.utc)
    else:
        ref = now.replace(tzinfo=timezone.utc)
    d: date = ref.date()
    start = datetime(d.year, d.month, d.day)
    end = start + timedelta(days=1)
    return start, end


def to_utc_iso_z(dt: Optional[datetime]) -> Optional[str]:
    """
    Serialize a ``datetime`` for JSON. Naive values are treated as **UTC wall clock**.
    Always returns ``...Z`` (never a bare offset-less string without ``Z``).
    """
    if dt is None:
        return None
    if getattr(dt, "tzinfo", None):
        naive_utc = dt.astimezone(timezone.utc).replace(tzinfo=None)
    else:
        naive_utc = dt
    s = naive_utc.isoformat()
    return s if s.endswith("Z") else s + "Z"
