"""Canonical Scheduler timezone helpers (Wave 9).

Contract:
- Database / worker: naive UTC instants
- Owner input: local date/time + IANA timezone → convert once to UTC
- Owner display: convert UTC → profile/owner timezone
- No local-naive timestamps may enter scheduler storage

Uses ``zoneinfo.ZoneInfo`` only (no pytz).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Optional, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_OWNER_TIMEZONE = "Asia/Yerevan"


class SchedulerTimezoneError(ValueError):
    """Owner/API validation error for timezone or local datetime."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class OwnerLocalInstant:
    """Validated owner-local wall clock + timezone."""

    local_naive: datetime
    timezone_name: str
    utc_naive: datetime


def utc_now_naive() -> datetime:
    """Current UTC instant as naive datetime (scheduler DB contract)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def resolve_owner_timezone(name: Optional[str], *, fallback: str = DEFAULT_OWNER_TIMEZONE) -> str:
    """Return a validated IANA timezone name."""
    candidate = (name or "").strip() or fallback
    try:
        ZoneInfo(candidate)
    except ZoneInfoNotFoundError as exc:
        raise SchedulerTimezoneError(
            "INVALID_TIMEZONE",
            f"Unknown timezone '{candidate}'. Use an IANA name such as Asia/Yerevan.",
        ) from exc
    return candidate


def ensure_zoneinfo(name: Optional[str], *, fallback: str = DEFAULT_OWNER_TIMEZONE) -> ZoneInfo:
    return ZoneInfo(resolve_owner_timezone(name, fallback=fallback))


def _detect_dst_issue(local_naive: datetime, tz: ZoneInfo) -> Optional[str]:
    """Return 'NONEXISTENT_LOCAL_TIME' or 'AMBIGUOUS_LOCAL_TIME' when applicable."""
    if local_naive.tzinfo is not None:
        raise SchedulerTimezoneError(
            "LOCAL_MUST_BE_NAIVE",
            "Local datetime must be naive (timezone provided separately).",
        )
    fold0 = local_naive.replace(tzinfo=tz, fold=0)
    # Nonexistent (spring-forward gap): UTC round-trip wall clock differs.
    # Check this before ambiguous-fold detection — ZoneInfo may report unequal
    # offsets inside a gap as well.
    back = fold0.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None)
    if back.replace(microsecond=0) != local_naive.replace(microsecond=0):
        return "NONEXISTENT_LOCAL_TIME"
    fold1 = local_naive.replace(tzinfo=tz, fold=1)
    if fold0.utcoffset() != fold1.utcoffset():
        return "AMBIGUOUS_LOCAL_TIME"
    return None


def local_to_utc_naive(
    local_naive: datetime,
    timezone_name: str,
    *,
    fold: Optional[int] = None,
) -> datetime:
    """
    Convert owner-local naive datetime to naive UTC for storage.

    Rejects nonexistent DST gaps. Ambiguous times require explicit ``fold``
    (0=earlier, 1=later) or are rejected.
    """
    tz_name = resolve_owner_timezone(timezone_name)
    tz = ZoneInfo(tz_name)
    issue = _detect_dst_issue(local_naive, tz)
    if issue == "NONEXISTENT_LOCAL_TIME":
        raise SchedulerTimezoneError(
            issue,
            "That local time does not exist because of a daylight-saving transition.",
        )
    if issue == "AMBIGUOUS_LOCAL_TIME":
        if fold not in (0, 1):
            raise SchedulerTimezoneError(
                issue,
                "That local time is ambiguous (clocks fall back). "
                "Specify fold=0 (earlier) or fold=1 (later).",
            )
        aware = local_naive.replace(tzinfo=tz, fold=int(fold))
    else:
        aware = local_naive.replace(tzinfo=tz)
    return aware.astimezone(timezone.utc).replace(tzinfo=None)


def utc_to_local_naive(utc_naive: datetime, timezone_name: str) -> datetime:
    """Convert stored naive UTC to owner-local naive wall clock."""
    tz = ensure_zoneinfo(timezone_name)
    if utc_naive.tzinfo is not None:
        aware_utc = utc_naive.astimezone(timezone.utc)
    else:
        aware_utc = utc_naive.replace(tzinfo=timezone.utc)
    return aware_utc.astimezone(tz).replace(tzinfo=None)


def parse_owner_local_datetime(
    local_date: Union[str, date],
    local_time: Union[str, time],
    timezone_name: str,
    *,
    fold: Optional[int] = None,
) -> OwnerLocalInstant:
    """Parse owner local date+time+timezone into UTC storage instant."""
    if isinstance(local_date, str):
        try:
            d = date.fromisoformat(local_date.strip())
        except ValueError as exc:
            raise SchedulerTimezoneError(
                "INVALID_LOCAL_DATE",
                "local_date must be YYYY-MM-DD.",
            ) from exc
    else:
        d = local_date

    if isinstance(local_time, str):
        raw = local_time.strip()
        try:
            # Accept HH:MM or HH:MM:SS
            parts = raw.split(":")
            if len(parts) == 2:
                t = time(int(parts[0]), int(parts[1]))
            elif len(parts) == 3:
                t = time(int(parts[0]), int(parts[1]), int(parts[2]))
            else:
                raise ValueError("bad time")
        except ValueError as exc:
            raise SchedulerTimezoneError(
                "INVALID_LOCAL_TIME",
                "local_time must be HH:MM or HH:MM:SS.",
            ) from exc
    else:
        t = local_time

    local_naive = datetime(d.year, d.month, d.day, t.hour, t.minute, t.second)
    tz_name = resolve_owner_timezone(timezone_name)
    utc_naive = local_to_utc_naive(local_naive, tz_name, fold=fold)
    return OwnerLocalInstant(
        local_naive=local_naive,
        timezone_name=tz_name,
        utc_naive=utc_naive,
    )


def parse_aware_iso_to_utc_naive(value: str) -> datetime:
    """Parse explicit ISO-8601 datetime; require offset/Z; return naive UTC."""
    raw = (value or "").strip()
    if not raw:
        raise SchedulerTimezoneError("INVALID_DATETIME", "Datetime is required.")
    try:
        if raw.endswith("Z"):
            dt = datetime.fromisoformat(raw[:-1] + "+00:00")
        else:
            dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise SchedulerTimezoneError(
            "INVALID_DATETIME",
            "Datetime must be ISO-8601 (include Z or an offset).",
        ) from exc
    if dt.tzinfo is None:
        raise SchedulerTimezoneError(
            "NAIVE_DATETIME_REJECTED",
            "Naive datetimes are not accepted. Provide timezone or an offset/Z.",
        )
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def format_owner_local(dt_local_naive: datetime, timezone_name: str) -> str:
    """Owner-facing local display, e.g. '10 Sep 2026, 15:00 Asia/Yerevan'."""
    tz_name = resolve_owner_timezone(timezone_name)
    day = str(dt_local_naive.day)
    return f"{day} {dt_local_naive.strftime('%b %Y, %H:%M')} {tz_name}"


def owner_schedule_fields(
    utc_naive: Optional[datetime],
    timezone_name: Optional[str],
) -> dict:
    """Additive API/UI fields for a stored UTC run_at."""
    from src.core.datetime_utc import to_utc_iso_z

    tz_name = resolve_owner_timezone(timezone_name)
    if utc_naive is None:
        return {
            "scheduled_at_utc": None,
            "scheduled_at_local": None,
            "timezone": tz_name,
        }
    local = utc_to_local_naive(utc_naive, tz_name)
    return {
        "scheduled_at_utc": to_utc_iso_z(utc_naive),
        "scheduled_at_local": format_owner_local(local, tz_name),
        "timezone": tz_name,
    }
