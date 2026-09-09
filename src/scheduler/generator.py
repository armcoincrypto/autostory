"""
Job generator - creates scheduled_jobs for today (idempotent)
"""
import json
import random
from datetime import datetime, date, timedelta, timezone
from typing import Optional, List, Tuple
from zoneinfo import ZoneInfo

from src.core.database import get_db_context
from src.ai_agent.account_allowlist import account_id_excluded_from_scheduler_worker
from src.core.scheduler_models import (
    ScheduleProfile, ScheduleRule, ScheduledJob,
    AccountTargetBinding, ChatTarget, JobStatus
)
from src.clients.target_health import (
    classify_target,
    HEALTH_INVALID,
    is_health_allowed_for_send,
    merged_target_health_row,
)
from src.scheduler.generation_eligibility import (
    evaluate_generation_eligibility,
    log_generation_denied,
    log_generation_allowed,
)

from src.scheduler.timezone import DEFAULT_OWNER_TIMEZONE, SchedulerTimezoneError, local_to_utc_naive

DEFAULT_TIMEZONE = DEFAULT_OWNER_TIMEZONE


def generate_jobs_for_date(target_date: Optional[date] = None) -> int:
    """
    Create scheduled_jobs for one local calendar day per profile timezone.

    ``target_date`` is optional: when omitted (normal worker path), each profile's
    "today" is derived as the current calendar date in that profile's IANA zone.
    ``ScheduledJob.run_at`` is stored as **naive UTC** (historical schema).

    When ``target_date`` is set (tests/backfill), the same calendar day is used
    for every profile — callers should pass a date meaningful for all profiles.
    """
    created = 0
    pending_status = JobStatus.PENDING.value

    with get_db_context() as db:
        profiles = db.query(ScheduleProfile).filter(
            ScheduleProfile.is_enabled == True
        ).all()

        for profile in profiles:
            if account_id_excluded_from_scheduler_worker(db, int(profile.account_id)):
                continue
            tz_str = profile.timezone or DEFAULT_TIMEZONE
            tz = ZoneInfo(tz_str)
            local_day = (
                target_date
                if target_date is not None
                else datetime.now(timezone.utc).astimezone(tz).date()
            )
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

                jitter = profile.jitter_sec or 0

                # RANDOM:HH:MM-HH:MM format = one random time per target per day within window
                for time_str in times:
                    if not isinstance(time_str, str):
                        continue
                    if time_str.startswith("RANDOM"):
                        window_start, window_end = _parse_random_window(time_str)
                        if window_start is None or window_end is None:
                            continue
                        created += _create_random_jobs(
                            db, profile.account_id, targets, rule.type, local_day,
                            tz, window_start, window_end, jitter
                        )
                        continue

                    # Fixed time format HH:MM (local wall clock in profile TZ → UTC)
                    try:
                        h, m = map(int, time_str.split(":"))
                        local_naive = datetime(local_day.year, local_day.month, local_day.day, h, m)
                        run_at = local_to_utc_naive(local_naive, tz_str)

                        quiet_start, quiet_end = _parse_quiet_hours(profile.quiet_hours_json)
                        if quiet_start and quiet_end:
                            run_at_utc = run_at.replace(tzinfo=timezone.utc)
                            t_local = run_at_utc.astimezone(tz)
                            if _in_quiet_hours(t_local.time(), quiet_start, quiet_end):
                                continue

                        run_at = run_at + timedelta(seconds=random.randint(0, jitter))

                        for target_id in targets:
                            if _job_exists(db, profile.account_id, target_id, rule.type, local_day, tz):
                                continue
                            if not _try_insert_job(
                                db,
                                account_id=profile.account_id,
                                target_id=target_id,
                                msg_type=rule.type,
                                run_at=run_at,
                                local_day=local_day,
                                schedule_rule_id=getattr(rule, "id", None),
                                schedule_profile_id=getattr(profile, "id", None),
                            ):
                                continue
                            created += 1
                    except (ValueError, IndexError, SchedulerTimezoneError):
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


def _create_random_jobs(db, account_id, targets: List[int], msg_type: str, local_day: date,
                        tz: ZoneInfo, window_start: str, window_end: str, jitter: int) -> int:
    """Create exactly 1 job per target with random run_at within window. Idempotent."""
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

    for target_id in targets:
        if _job_exists(db, account_id, target_id, msg_type, local_day, tz):
            continue
        # Random minute within window (different each day via seed from date)
        rng = random.Random((local_day.toordinal() * 1000 + account_id * 100 + target_id))
        rand_min = rng.randint(start_min, end_min - 1) if end_min > start_min + 1 else start_min
        h, m = divmod(rand_min, 60)
        run_at = datetime(local_day.year, local_day.month, local_day.day, h, m, tzinfo=tz)
        run_at = run_at.astimezone(timezone.utc).replace(tzinfo=None)
        run_at = run_at + timedelta(seconds=rng.randint(0, jitter) if jitter else 0)
        if _try_insert_job(
            db,
            account_id=account_id,
            target_id=target_id,
            msg_type=msg_type,
            run_at=run_at,
            local_day=local_day,
        ):
            created += 1
    return created


def _try_insert_job(
    db,
    *,
    account_id: int,
    target_id: int,
    msg_type: str,
    run_at: datetime,
    local_day: date,
    schedule_rule_id: Optional[int] = None,
    schedule_profile_id: Optional[int] = None,
    generation_scope: str = "normal",
    job_marker: Optional[str] = None,
) -> bool:
    """Insert one PENDING job when eligibility passes; return True if inserted."""
    decision = evaluate_generation_eligibility(
        db,
        job_type=msg_type,
        account_id=int(account_id),
        target_id=int(target_id),
        generation_scope=generation_scope,
        generator_date=local_day,
        job_marker=job_marker,
        schedule_rule_id=schedule_rule_id,
        schedule_profile_id=schedule_profile_id,
    )
    if not decision.allowed:
        log_generation_denied(decision, generator_date=local_day)
        return False

    pending_status = JobStatus.PENDING.value
    job = ScheduledJob(
        account_id=int(account_id),
        target_id=int(target_id),
        type=msg_type,
        run_at=run_at,
        status=pending_status,
    )
    db.add(job)
    db.flush()
    log_generation_allowed(
        job_type=msg_type,
        account_id=int(account_id),
        target_id=int(target_id),
        generator_date=local_day,
        job_id=getattr(job, "id", None),
    )
    return True


def _job_exists(
    db,
    account_id: int,
    target_id: int,
    msg_type: str,
    local_day: date,
    tz: ZoneInfo,
) -> bool:
    """
    True if a pending job already exists whose ``run_at`` falls on ``local_day``
    in timezone ``tz`` (compared using naive UTC instants in the DB).
    """
    start_local = datetime(local_day.year, local_day.month, local_day.day, 0, 0, 0, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    day_start_utc = start_local.astimezone(timezone.utc).replace(tzinfo=None)
    day_end_utc = end_local.astimezone(timezone.utc).replace(tzinfo=None)
    pending_status = JobStatus.PENDING.value
    # Treat SKIPPED/SENT/RUNNING same-day rows as present so neutralized jobs are not regenerated.
    active_statuses = (
        pending_status,
        JobStatus.SKIPPED.value,
        JobStatus.SENT.value,
        JobStatus.RUNNING.value,
    )
    return db.query(ScheduledJob).filter(
        ScheduledJob.account_id == account_id,
        ScheduledJob.target_id == target_id,
        ScheduledJob.type == msg_type,
        ScheduledJob.run_at >= day_start_utc,
        ScheduledJob.run_at < day_end_utc,
        ScheduledJob.status.in_(active_statuses),
    ).first() is not None


def _filter_scheduler_target_ids(db, target_ids: List[int], account_id: int) -> List[int]:
    """Drop invalid or operationally non-sendable targets for this account."""
    if not target_ids:
        return []
    uniq = list(dict.fromkeys(int(x) for x in target_ids))
    rows = db.query(ChatTarget).filter(ChatTarget.id.in_(uniq)).all()
    by_id = {r.id: r for r in rows}
    out: List[int] = []
    aid = int(account_id)
    for tid in uniq:
        row = by_id.get(tid)
        if not row:
            continue
        if classify_target(row).get("health") == HEALTH_INVALID:
            continue
        mh = merged_target_health_row(db, row, aid)
        if not is_health_allowed_for_send(mh["health"]):
            continue
        out.append(tid)
    return out


def _resolve_targets(db, rule, account_id) -> list:
    """Get target IDs for this rule"""
    if rule.target_mode == "ONLY_SELECTED":
        try:
            ids = json.loads(rule.selected_target_ids_json or "[]")
            raw = ids if isinstance(ids, list) else []
        except json.JSONDecodeError:
            return []
        return _filter_scheduler_target_ids(db, raw, int(account_id))

    bindings = db.query(AccountTargetBinding).filter(
        AccountTargetBinding.account_id == account_id,
        AccountTargetBinding.can_post == True
    ).all()
    raw = [
        b.target_id for b in bindings
        if rule.type in (b.allowed_types or "PROMO,INFO").replace(" ", "").split(",")
    ]
    return _filter_scheduler_target_ids(db, raw, int(account_id))


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
