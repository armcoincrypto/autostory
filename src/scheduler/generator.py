"""
Job generator - creates scheduled_jobs for today (idempotent)
"""
import json
import random
from datetime import datetime, date, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from src.core.database import get_db_context
from src.core.scheduler_models import (
    ScheduleProfile, ScheduleRule, ScheduledJob,
    AccountTargetBinding, ChatTarget, JobStatus
)

TIMEZONE = "Asia/Yerevan"


def generate_jobs_for_date(target_date: date) -> int:
    """Create scheduled_jobs for the given date. Idempotent - skips existing."""
    tz = ZoneInfo(TIMEZONE)
    created = 0

    with get_db_context() as db:
        profiles = db.query(ScheduleProfile).filter(
            ScheduleProfile.is_enabled == True
        ).all()

        for profile in profiles:
            rules = db.query(ScheduleRule).filter(
                ScheduleRule.account_id == profile.account_id,
                ScheduleRule.is_enabled == True
            ).all()

            for rule in rules:
                try:
                    times = json.loads(rule.times_json) if isinstance(rule.times_json, str) else rule.times_json
                except (json.JSONDecodeError, TypeError):
                    times = []

                targets = _resolve_targets(db, rule, profile.account_id)
                if not targets:
                    continue

                quiet_start, quiet_end = _parse_quiet_hours(profile.quiet_hours_json)
                jitter = profile.jitter_sec or 0

                for time_str in times:
                    try:
                        h, m = map(int, time_str.split(":"))
                        from datetime import timezone
                        run_at = datetime(target_date.year, target_date.month, target_date.day, h, m, tzinfo=tz)
                        run_at = run_at.astimezone(timezone.utc).replace(tzinfo=None)  # store as naive UTC

                        if quiet_start and quiet_end:
                            t = run_at.time() if hasattr(run_at, 'time') else run_at
                            if _in_quiet_hours(t, quiet_start, quiet_end):
                                continue

                        run_at = run_at + timedelta(seconds=random.randint(0, jitter))

                        for target_id in targets:
                            existing = db.query(ScheduledJob).filter(
                                ScheduledJob.account_id == profile.account_id,
                                ScheduledJob.target_id == target_id,
                                ScheduledJob.type == rule.type,
                                ScheduledJob.run_at >= datetime(target_date.year, target_date.month, target_date.day),
                                ScheduledJob.run_at < datetime(target_date.year, target_date.month, target_date.day) + timedelta(days=1),
                                ScheduledJob.status == JobStatus.PENDING
                            ).first()
                            if existing:
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


def _resolve_targets(db, rule, account_id) -> list:
    """Get target IDs for this rule"""
    if rule.target_mode == "ONLY_SELECTED":
        try:
            ids = json.loads(rule.selected_target_ids_json or "[]")
            return ids if isinstance(ids, list) else []
        except json.JSONDecodeError:
            return []

    bindings = db.query(AccountTargetBinding).filter(
        AccountTargetBinding.account_id == account_id,
        AccountTargetBinding.can_post == True
    ).all()
    return [
        b.target_id for b in bindings
        if rule.type in (b.allowed_types or "PROMO,INFO").replace(" ", "").split(",")
    ]


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
