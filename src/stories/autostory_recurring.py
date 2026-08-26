"""Recurring daily AutoStory targets — durable per-account-per-day progress.

Legacy campaigns use campaign_mode=accounts_publish_once (unchanged).
New campaigns use campaign_mode=recurring_daily.
"""
from __future__ import annotations

import math
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import structlog

logger = structlog.get_logger(__name__)

CAMPAIGN_MODE_ONCE = "accounts_publish_once"
CAMPAIGN_MODE_RECURRING = "recurring_daily"

# Platform daily capacity defaults to 1 until daily_story_limit is raised.
MAX_STORIES_PER_ACCOUNT_PER_DAY = int(
    os.environ.get("MAX_STORIES_PER_ACCOUNT_PER_DAY") or "1"
)
DEFAULT_AWAKE_START = "10:00"
DEFAULT_AWAKE_END = "20:00"


def clamp_stories_per_account_per_day(raw: Any) -> int:
    try:
        n = int(raw if raw not in (None, "") else 1)
    except (TypeError, ValueError):
        n = 1
    return max(1, min(MAX_STORIES_PER_ACCOUNT_PER_DAY, n))


def campaign_mode_of(campaign: Any) -> str:
    if isinstance(campaign, dict):
        mode = campaign.get("campaign_mode")
    else:
        mode = getattr(campaign, "campaign_mode", None)
    mode = str(mode or CAMPAIGN_MODE_ONCE).strip().lower()
    if mode == CAMPAIGN_MODE_RECURRING:
        return CAMPAIGN_MODE_RECURRING
    return CAMPAIGN_MODE_ONCE


def is_recurring(campaign: Any) -> bool:
    return campaign_mode_of(campaign) == CAMPAIGN_MODE_RECURRING


def max_story_publishes(
    account_count: int,
    stories_per_account_per_day: int,
    duration_days: int,
) -> int:
    return max(0, int(account_count)) * max(1, int(stories_per_account_per_day)) * max(
        1, int(duration_days)
    )


def operator_timezone_name() -> str:
    try:
        from config.settings import settings

        return str(getattr(settings, "sched_default_timezone", None) or "UTC").strip() or "UTC"
    except Exception:
        return "UTC"


def campaign_local_today(*, now: datetime | None = None, tz_name: str | None = None) -> date:
    tz_name = tz_name or operator_timezone_name()
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(tz).date()


def parse_hhmm(value: str | None, default: str) -> tuple[int, int]:
    raw = str(value or default).strip() or default
    parts = raw.split(":")
    try:
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        dparts = default.split(":")
        return int(dparts[0]), int(dparts[1])


def awake_bounds(campaign: Any | None = None) -> tuple[str, str]:
    if campaign is None:
        return DEFAULT_AWAKE_START, DEFAULT_AWAKE_END
    if isinstance(campaign, dict):
        start = campaign.get("awake_start_hhmm") or DEFAULT_AWAKE_START
        end = campaign.get("awake_end_hhmm") or DEFAULT_AWAKE_END
    else:
        start = getattr(campaign, "awake_start_hhmm", None) or DEFAULT_AWAKE_START
        end = getattr(campaign, "awake_end_hhmm", None) or DEFAULT_AWAKE_END
    sh, sm = parse_hhmm(start, DEFAULT_AWAKE_START)
    eh, em = parse_hhmm(end, DEFAULT_AWAKE_END)
    start_m = sh * 60 + sm
    end_m = eh * 60 + em
    if end_m <= start_m:
        return DEFAULT_AWAKE_START, DEFAULT_AWAKE_END
    return f"{sh:02d}:{sm:02d}", f"{eh:02d}:{em:02d}"


def compute_pickup_opportunities(
    *,
    account_count: int,
    stories_per_account_per_day: int,
    max_wave: int = 25,
) -> int:
    """How many HH:MM slots/day to spread work across the awake window."""
    from src.stories.autostory_hardening import MAX_AUTOSTORY_WAVE_SIZE

    wave = max(1, int(max_wave or MAX_AUTOSTORY_WAVE_SIZE))
    waves_needed = max(1, math.ceil(max(1, account_count) / wave))
    spad = max(1, int(stories_per_account_per_day))
    return max(1, min(12, waves_needed * spad))


def spread_local_times(*, start_hhmm: str, end_hhmm: str, count: int) -> list[str]:
    sh, sm = parse_hhmm(start_hhmm, DEFAULT_AWAKE_START)
    eh, em = parse_hhmm(end_hhmm, DEFAULT_AWAKE_END)
    start_m = sh * 60 + sm
    end_m = eh * 60 + em
    n = max(1, min(12, int(count)))
    span = end_m - start_m
    if n == 1:
        mid = start_m + span // 2
        return [f"{mid // 60:02d}:{mid % 60:02d}"]
    out: list[str] = []
    seen: set[str] = set()
    for i in range(n):
        mins = start_m + int(round(span * i / (n - 1)))
        hhmm = f"{mins // 60:02d}:{mins % 60:02d}"
        if hhmm not in seen:
            seen.add(hhmm)
            out.append(hhmm)
    return out


def local_times_to_utc_hhmm(local_times: list[str], *, tz_name: str | None = None) -> list[str]:
    tz_name = tz_name or operator_timezone_name()
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    utc_times: list[str] = []
    seen: set[str] = set()
    local_now = datetime.now(tz)
    for lt in local_times:
        hh, mm = parse_hhmm(lt, "12:00")
        local_dt = local_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        utc_dt = local_dt.astimezone(timezone.utc)
        ut = f"{utc_dt.hour:02d}:{utc_dt.minute:02d}"
        if ut not in seen:
            seen.add(ut)
            utc_times.append(ut)
    return utc_times


def plan_recurring_times_json(
    *,
    account_count: int,
    stories_per_account_per_day: int,
    awake_start: str | None = None,
    awake_end: str | None = None,
    tz_name: str | None = None,
) -> dict[str, Any]:
    start = awake_start or DEFAULT_AWAKE_START
    end = awake_end or DEFAULT_AWAKE_END
    n = compute_pickup_opportunities(
        account_count=account_count,
        stories_per_account_per_day=stories_per_account_per_day,
    )
    local_times = spread_local_times(start_hhmm=start, end_hhmm=end, count=n)
    utc_times = local_times_to_utc_hhmm(local_times, tz_name=tz_name)
    return {
        "schedule_mode": "awake_window",
        "times_json": utc_times,
        "local_times_json": local_times,
        "explicit": False,
        "awake_window": f"{start}-{end}",
        "awake_start_hhmm": start,
        "awake_end_hhmm": end,
        "pickup_opportunities": n,
        "timezone": tz_name or operator_timezone_name(),
    }


def campaign_local_dates(
    *,
    started_at: datetime | None,
    duration_days: int,
    tz_name: str | None = None,
) -> list[date]:
    tz_name = tz_name or operator_timezone_name()
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    if started_at is None:
        start_local = datetime.now(tz).date()
    else:
        st = started_at
        if st.tzinfo is None:
            st = st.replace(tzinfo=timezone.utc)
        start_local = st.astimezone(tz).date()
    days = max(1, int(duration_days))
    return [start_local + timedelta(days=i) for i in range(days)]


def ensure_daily_progress_rows(
    db,
    *,
    campaign_id: int,
    account_ids: list[int],
    local_dates: list[date],
    target_count: int,
) -> int:
    from src.core.models import AutoStoryDailyProgress

    created = 0
    spad = max(1, int(target_count))
    for d in local_dates:
        for aid in account_ids:
            row = (
                db.query(AutoStoryDailyProgress)
                .filter_by(
                    campaign_id=int(campaign_id),
                    account_id=int(aid),
                    local_date=d,
                )
                .one_or_none()
            )
            if row is None:
                row = AutoStoryDailyProgress(
                    campaign_id=int(campaign_id),
                    account_id=int(aid),
                    local_date=d,
                    target_count=spad,
                    successful_count=0,
                    failed_count=0,
                    ambiguous_count=0,
                    completed_for_day=False,
                )
                db.add(row)
                created += 1
            else:
                # Never reduce target below already-successful count
                row.target_count = max(int(row.target_count or 0), spad, int(row.successful_count or 0))
    db.flush()
    return created


def get_daily_row(db, *, campaign_id: int, account_id: int, local_date: date):
    from src.core.models import AutoStoryDailyProgress

    return (
        db.query(AutoStoryDailyProgress)
        .filter_by(
            campaign_id=int(campaign_id),
            account_id=int(account_id),
            local_date=local_date,
        )
        .one_or_none()
    )


def remaining_today(
    db,
    *,
    campaign_id: int,
    account_id: int,
    local_date: date | None = None,
) -> int:
    d = local_date or campaign_local_today()
    row = get_daily_row(db, campaign_id=campaign_id, account_id=account_id, local_date=d)
    if row is None:
        return 0
    if int(row.ambiguous_count or 0) > 0:
        return 0  # fail-closed: ambiguous blocks further attempts that day
    if row.completed_for_day:
        return 0
    return max(0, int(row.target_count or 0) - int(row.successful_count or 0))


def _refresh_completed(row) -> None:
    if int(row.ambiguous_count or 0) > 0:
        row.completed_for_day = True
        return
    row.completed_for_day = int(row.successful_count or 0) >= int(row.target_count or 0)


def record_daily_success(
    db,
    *,
    campaign_id: int,
    account_id: int,
    local_date: date | None = None,
    story_id: int | None = None,
) -> dict[str, Any]:
    d = local_date or campaign_local_today()
    row = get_daily_row(db, campaign_id=campaign_id, account_id=account_id, local_date=d)
    if row is None:
        return {"ok": False, "error": "daily_row_missing"}
    if int(row.ambiguous_count or 0) > 0:
        return {"ok": False, "error": "ambiguous_blocks_retry", "remaining": 0}
    if int(row.successful_count or 0) >= int(row.target_count or 0):
        row.completed_for_day = True
        return {"ok": False, "error": "daily_target_already_met", "remaining": 0}
    row.successful_count = int(row.successful_count or 0) + 1
    row.last_attempt_at = datetime.utcnow()
    if story_id is not None:
        row.last_story_id = int(story_id)
    _refresh_completed(row)
    rem = max(0, int(row.target_count) - int(row.successful_count))
    return {"ok": True, "successful_count": int(row.successful_count), "remaining": rem}


def record_daily_ambiguous(
    db,
    *,
    campaign_id: int,
    account_id: int,
    local_date: date | None = None,
) -> None:
    d = local_date or campaign_local_today()
    row = get_daily_row(db, campaign_id=campaign_id, account_id=account_id, local_date=d)
    if row is None:
        return
    row.ambiguous_count = int(row.ambiguous_count or 0) + 1
    row.last_attempt_at = datetime.utcnow()
    row.completed_for_day = True  # do not blindly retry


def record_daily_failed(
    db,
    *,
    campaign_id: int,
    account_id: int,
    local_date: date | None = None,
) -> None:
    d = local_date or campaign_local_today()
    row = get_daily_row(db, campaign_id=campaign_id, account_id=account_id, local_date=d)
    if row is None:
        return
    row.failed_count = int(row.failed_count or 0) + 1
    row.last_attempt_at = datetime.utcnow()


def campaign_daily_progress_summary(db, campaign_id: int) -> dict[str, Any]:
    from src.core.models import AutoStoryDailyProgress

    rows = (
        db.query(AutoStoryDailyProgress)
        .filter(AutoStoryDailyProgress.campaign_id == int(campaign_id))
        .all()
    )
    success = sum(int(r.successful_count or 0) for r in rows)
    target = sum(int(r.target_count or 0) for r in rows)
    remaining = sum(
        max(0, int(r.target_count or 0) - int(r.successful_count or 0))
        for r in rows
        if int(r.ambiguous_count or 0) == 0 and not r.completed_for_day
    )
    incomplete = [
        r
        for r in rows
        if not r.completed_for_day and int(r.ambiguous_count or 0) == 0
        and int(r.successful_count or 0) < int(r.target_count or 0)
    ]
    return {
        "successful_count": success,
        "target_count": target,
        "remaining_count": remaining,
        "row_count": len(rows),
        "incomplete_rows": len(incomplete),
        "all_complete": len(rows) > 0 and len(incomplete) == 0,
    }


def select_next_recurring_wave_accounts(
    db,
    campaign: Any,
    *,
    wave_index: int,
) -> dict[str, Any]:
    """Eligible accounts with remaining daily target for local today."""
    from src.stories.autostory_hardening import (
        MAX_AUTOSTORY_WAVE_SIZE,
        account_has_daily_capacity,
        ensure_progress_row,
        is_account_certified_publish,
        rotate_account_ids,
    )

    all_ids = [int(x) for x in (campaign.account_ids or [])]
    rotated = rotate_account_ids(db, all_ids)
    today = campaign_local_today()
    spad = clamp_stories_per_account_per_day(
        getattr(campaign, "stories_per_account_per_day", 1)
    )
    dates = campaign_local_dates(
        started_at=getattr(campaign, "started_at", None),
        duration_days=int(getattr(campaign, "duration_days", None) or 1),
    )
    ensure_daily_progress_rows(
        db,
        campaign_id=int(campaign.id),
        account_ids=all_ids,
        local_dates=dates,
        target_count=spad,
    )

    eligible: list[int] = []
    blocked: list[dict[str, Any]] = []
    unfinished: list[int] = []
    today_remaining_accounts = 0
    capacity_blocked_today = 0

    for aid in rotated:
        rem = remaining_today(
            db, campaign_id=int(campaign.id), account_id=aid, local_date=today
        )
        # Also count future incomplete days toward unfinished
        future_rem = 0
        for d in dates:
            if d < today:
                continue
            future_rem += remaining_today(
                db, campaign_id=int(campaign.id), account_id=aid, local_date=d
            )
        if future_rem > 0:
            unfinished.append(aid)
        if rem <= 0:
            continue
        today_remaining_accounts += 1
        cert_ok, cert_reason = is_account_certified_publish(db, aid)
        if not cert_ok:
            blocked.append({"account_id": aid, "reason": cert_reason})
            continue
        cap_ok, cap_reason = account_has_daily_capacity(db, aid)
        if not cap_ok:
            capacity_blocked_today += 1
            blocked.append({"account_id": aid, "reason": cap_reason})
            ensure_progress_row(
                db,
                campaign_id=int(campaign.id),
                wave_index=wave_index,
                account_id=aid,
                status="deferred",
            )
            continue
        eligible.append(aid)
        if len(eligible) >= MAX_AUTOSTORY_WAVE_SIZE:
            break

    return {
        "wave_account_ids": eligible,
        "blocked": blocked,
        "unfinished_count": len(unfinished),
        "remaining_after_wave": max(0, len(unfinished) - len(eligible)),
        "today_remaining_accounts": today_remaining_accounts,
        "capacity_blocked_today": capacity_blocked_today,
        "wave_index": int(wave_index),
        "max_wave_size": MAX_AUTOSTORY_WAVE_SIZE,
        "local_date": today.isoformat(),
        "recurring": True,
    }


def recurring_campaign_complete(db, campaign: Any) -> bool:
    summary = campaign_daily_progress_summary(db, int(campaign.id))
    if summary["row_count"] == 0:
        return False
    return bool(summary["all_complete"])
