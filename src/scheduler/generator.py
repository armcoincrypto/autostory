"""
Job generator - creates scheduled_jobs for today (idempotent)
"""
import json
import random
from datetime import datetime, date, timedelta, timezone
from typing import Optional, List, Tuple
from zoneinfo import ZoneInfo

from src.core.database import get_db_context
from src.core.scheduler_models import (
    ScheduleProfile, ScheduleRule, ScheduledJob,
    AccountTargetBinding, ChatTarget, JobStatus
)

DEFAULT_TIMEZONE = "Europe/Moscow"


def generate_jobs_for_date(target_date: date) -> int:
    """Create scheduled_jobs for the given date. Idempotent - skips existing."""
    created = 0

    with get_db_context() as db:
        profiles = db.query(ScheduleProfile).filter(
            ScheduleProfile.is_enabled == True
        ).all()

        for profile in profiles:
            tz_str = profile.timezone or DEFAULT_TIMEZONE
            tz = ZoneInfo(tz_str)
            rules = db.query(ScheduleRule).filter(
                ScheduleRule.account_id == profile.account_id,
                ScheduleRule.is_enabled == True
            ).all()

            for rule in rules:
                try:
                    times = json.loads(rule.times_json) if isinstance(rule.times_json, str) else rule.times_json
                except (json.JSONDecodeError, TypeError):
                    times = []

                targets_with_caps = _resolve_targets_with_caps(db, rule, profile.account_id)
                if not targets_with_caps:
                    continue

                jitter = profile.jitter_sec or 0

                # RANDOM:HH:MM-HH:MM format = N random times per target per day (N = binding.daily_cap or 1)
                for time_str in times:
                    if not isinstance(time_str, str):
                        continue
                    if time_str.startswith("RANDOM"):
                        window_start, window_end = _parse_random_window(time_str)
                        if window_start is None or window_end is None:
                            continue
                        created += _create_random_jobs(
                            db, profile.account_id, targets_with_caps, rule.type, target_date,
                            tz, window_start, window_end, jitter
                        )
                        continue

                    # Fixed time format HH:MM
                    try:
                        h, m = map(int, time_str.split(":"))
                        run_at = datetime(target_date.year, target_date.month, target_date.day, h, m, tzinfo=tz)
                        run_at = run_at.astimezone(timezone.utc).replace(tzinfo=None)

                        quiet_start, quiet_end = _parse_quiet_hours(profile.quiet_hours_json)
                        if quiet_start and quiet_end:
                            run_at_utc = run_at.replace(tzinfo=timezone.utc)
                            t_local = run_at_utc.astimezone(tz)
                            if _in_quiet_hours(t_local.time(), quiet_start, quiet_end):
                                continue

                        run_at = run_at + timedelta(seconds=random.randint(0, jitter))

                        for target_id, _ in targets_with_caps:
                            if _job_exists(db, profile.account_id, target_id, rule.type, target_date):
                                continue
                            job = ScheduledJob(
                                account_id=profile.account_id,
                                target_id=target_id,
                                type=rule.type,
                                run_at=run_at,
                                status=JobStatus.PENDING
                            )
                            db.add(job)
                            created += 1
                    except (ValueError, IndexError):
                        continue

    return created


def _parse_random_window(s: str) -> Tuple[Optional[str], Optional[str]]:
    """Parse RANDOM or RANDOM:HH:MM-HH:MM. Returns (start, end) or (None, None)."""
    if s == "RANDOM":
        return "10:00", "21:00"
    if s.startswith("RANDOM:"):
        part = s[7:].strip()
        if "-" in part:
            start, end = part.split("-", 1)
            return start.strip(), end.strip()
    return None, None


def _create_random_jobs(db, account_id, targets_with_caps: List[Tuple[int, int]], msg_type: str, target_date: date,
                        tz: ZoneInfo, window_start: str, window_end: str, jitter: int) -> int:
    """Create N jobs per target (N = daily_cap or 1) with random run_at within window. Idempotent."""
    created = 0
    try:
        sh, sm = map(int, window_start.split(":"))
        eh, em = map(int, window_end.split(":"))
        start_min = sh * 60 + sm
        end_min = eh * 60 + em
        if start_min >= end_min:
            return 0
    except (ValueError, AttributeError):
        return 0

    for target_id, daily_cap in targets_with_caps:
        cap = max(1, int(daily_cap)) if daily_cap is not None else 1
        existing = _job_count(db, account_id, target_id, msg_type, target_date)
        to_create = max(0, cap - existing)
        if to_create == 0:
            continue
        rng = random.Random((target_date.toordinal() * 1000 + account_id * 100 + target_id))
        used_minutes = set()
        for _ in range(to_create):
            rand_min = rng.randint(start_min, end_min - 1) if end_min > start_min + 1 else start_min
            for _ in range(50):
                if rand_min not in used_minutes:
                    break
                rand_min = rng.randint(start_min, end_min - 1) if end_min > start_min + 1 else start_min
            used_minutes.add(rand_min)
            h, m = divmod(rand_min, 60)
            run_at = datetime(target_date.year, target_date.month, target_date.day, h, m, tzinfo=tz)
            run_at = run_at.astimezone(timezone.utc).replace(tzinfo=None)
            run_at = run_at + timedelta(seconds=rng.randint(0, jitter) if jitter else 0)
            job = ScheduledJob(
                account_id=account_id,
                target_id=target_id,
                type=msg_type,
                run_at=run_at,
                status=JobStatus.PENDING
            )
            db.add(job)
            created += 1
    return created


def _job_count(db, account_id: int, target_id: int, msg_type: str, target_date: date) -> int:
    """Count existing jobs for (account, target, type, date)."""
    day_start = datetime(target_date.year, target_date.month, target_date.day, tzinfo=timezone.utc).replace(tzinfo=None)
    day_end = day_start + timedelta(days=1)
    return db.query(ScheduledJob).filter(
        ScheduledJob.account_id == account_id,
        ScheduledJob.target_id == target_id,
        ScheduledJob.type == msg_type,
        ScheduledJob.run_at >= day_start,
        ScheduledJob.run_at < day_end,
    ).count()


def _job_exists(db, account_id, target_id, msg_type, target_date) -> bool:
    """One job per (account, target, type, date). Skip if any exists (PENDING/SENT/FAILED)."""
    # Use UTC boundaries to match run_at (stored in UTC)
    day_start = datetime(target_date.year, target_date.month, target_date.day, tzinfo=timezone.utc).replace(tzinfo=None)
    day_end = day_start + timedelta(days=1)
    return db.query(ScheduledJob).filter(
        ScheduledJob.account_id == account_id,
        ScheduledJob.target_id == target_id,
        ScheduledJob.type == msg_type,
        ScheduledJob.run_at >= day_start,
        ScheduledJob.run_at < day_end,
    ).first() is not None


def _resolve_targets(db, rule, account_id) -> list:
    """Get target IDs for this rule. Excludes targets whose binding has can_post=False."""
    targets_with_caps = _resolve_targets_with_caps(db, rule, account_id)
    return [t for t, _ in targets_with_caps]


def _resolve_targets_with_caps(db, rule, account_id) -> List[Tuple[int, Optional[int]]]:
    """Get (target_id, daily_cap) for this rule. daily_cap is binding.daily_cap (1 if None)."""
    bindings = db.query(AccountTargetBinding).filter(
        AccountTargetBinding.account_id == account_id,
        AccountTargetBinding.can_post == True
    ).all()
    type_ok = lambda b: rule.type in (b.allowed_types or "PROMO,INFO").replace(" ", "").split(",")

    if rule.target_mode == "ONLY_SELECTED":
        try:
            ids = json.loads(rule.selected_target_ids_json or "[]")
            selected = ids if isinstance(ids, list) else []
        except json.JSONDecodeError:
            selected = []
        binding_map = {b.target_id: (b.daily_cap) for b in bindings if type_ok(b)}
        return [(t, binding_map.get(t)) for t in selected if t in binding_map]

    return [(b.target_id, b.daily_cap) for b in bindings if type_ok(b)]


def _parse_quiet_hours(json_str: Optional[str]) -> tuple:
    """Return (start_time, end_time) or (None, None)"""
    if not json_str:
        return None, None
    try:
        d = json.loads(json_str)
        return d.get("start"), d.get("end")
    except (json.JSONDecodeError, TypeError):
        return None, None


def _in_quiet_hours(t, start_str: str, end_str: str) -> bool:
    """Check if time t is within quiet hours"""
    try:
        sh, sm = map(int, start_str.split(":"))
        eh, em = map(int, end_str.split(":"))
        start_min = sh * 60 + sm
        end_min = eh * 60 + em
        if hasattr(t, 'hour'):
            now_min = t.hour * 60 + t.minute
        else:
            now_min = 0
        if start_min <= end_min:
            return start_min <= now_min < end_min
        return now_min >= start_min or now_min < end_min
    except (ValueError, AttributeError):
        return False
