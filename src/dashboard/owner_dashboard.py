"""Owner Dashboard snapshot (Wave I).

Cheap, fail-closed aggregates for the home page. No Telegram I/O, no OpenAI,
no N+1 account loops. Panel failures degrade independently.
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from src.dashboard.accounts_owner_health import (
    build_owner_health_index,
    owner_summary_from_index,
)
from src.messaging.message_draft_flags import messages_ai_draft_enabled
from src.messaging.scheduled_dm_flags import scheduled_dm_enabled

# Owner-facing windows (not lifetime history clutter)
_MESSAGE_LOOKBACK_DAYS = 7
_SCHEDULER_LOOKBACK_DAYS = 7
_BACKUP_MAX_AGE_HOURS = float(os.environ.get("MAX_AGE_HOURS") or os.environ.get("STORYFLEET_BACKUP_MAX_AGE_HOURS") or "36")
_DISK_WARN_PCT = int(os.environ.get("STORYFLEET_DISK_WARN_PCT") or "80")
_DISK_CRIT_PCT = int(os.environ.get("STORYFLEET_DISK_CRIT_PCT") or "90")
_BACKUP_STATUS_PATH = Path(
    os.environ.get("STORYFLEET_OFFHOST_STATUS")
    or "/opt/autostory/data/runtime/offhost_backup_status.json"
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def humanize_age(seconds: Optional[float]) -> str:
    if seconds is None:
        return "unknown"
    sec = max(0, int(seconds))
    if sec < 90:
        return "just now"
    minutes = sec // 60
    if minutes < 90:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def _panel_error(label: str) -> dict[str, Any]:
    return {
        "ok": False,
        "unavailable": True,
        "owner_copy": label,
        "attention": True,
    }


def _accounts_panel() -> dict[str, Any]:
    try:
        index = build_owner_health_index()
        summary = owner_summary_from_index(index)
        freshness = index.get("freshness") or {}
        certified = int(summary.get("owner_certified") or 0)
        needs = int(summary.get("owner_needs_attention") or 0)
        disabled = int(summary.get("owner_disabled") or 0)
        # Owner "Unavailable" excludes Disabled (shown separately): protected + reserved.
        unavailable_raw = int(summary.get("owner_unavailable") or 0)
        unavailable = max(0, unavailable_raw - disabled)
        empty = needs == 0
        return {
            "ok": True,
            "unavailable": False,
            "certified": certified,
            "needs_attention": needs,
            "disabled": disabled,
            "unavailable_count": unavailable,
            "matrix_fresh": bool(summary.get("owner_matrix_fresh")),
            "empty_attention": empty,
            "owner_copy": (
                "No accounts need attention"
                if empty
                else (
                    f"{needs} account needs attention"
                    if needs == 1
                    else f"{needs} accounts need attention"
                )
            ),
            "attention": needs > 0 or not bool(summary.get("owner_matrix_fresh")),
            "link": "/accounts",
            "link_label": "View Accounts",
            "freshness_age_seconds": freshness.get("age_seconds"),
        }
    except Exception:
        return _panel_error("Account health unavailable")


def _fleet_panel() -> dict[str, Any]:
    try:
        from src.stories.fleet_readiness_matrix import load_latest_matrix, matrix_freshness

        matrix = load_latest_matrix()
        freshness = matrix_freshness(matrix)
        age = freshness.get("age_seconds")
        fresh = bool(freshness.get("fresh")) and bool(freshness.get("present"))
        if not freshness.get("present"):
            copy = "Fleet health unavailable"
            attention = True
        elif not fresh:
            copy = "Fleet health needs refresh"
            attention = True
        else:
            copy = f"Fleet health updated {humanize_age(age)}"
            attention = False
        return {
            "ok": True,
            "unavailable": not bool(freshness.get("present")),
            "fresh": fresh,
            "generated_at": freshness.get("generated_at"),
            "age_seconds": age,
            "age_human": humanize_age(age),
            "owner_copy": copy,
            "attention": attention,
        }
    except Exception:
        return _panel_error("Fleet health unavailable")


def _messages_panel(*, db) -> dict[str, Any]:
    try:
        from sqlalchemy import func, text
        from src.messaging.models import OwnerDmIntent

        since = _utcnow() - timedelta(days=_MESSAGE_LOOKBACK_DAYS)
        since_naive = since.replace(tzinfo=None)
        try:
            rows = (
                db.query(OwnerDmIntent.status, func.count(OwnerDmIntent.id))
                .filter(OwnerDmIntent.created_at >= since_naive)
                .group_by(OwnerDmIntent.status)
                .all()
            )
            counts = {str(s or "").upper(): int(c) for s, c in rows}
        except Exception:
            rows = db.execute(
                text(
                    "SELECT upper(status), count(*) FROM owner_dm_intents "
                    "WHERE created_at >= :since GROUP BY upper(status)"
                ),
                {"since": since_naive.isoformat(sep=" ")},
            ).fetchall()
            counts = {str(s or "").upper(): int(c) for s, c in rows}

        sent = int(counts.get("SENT") or 0)
        failed = int(counts.get("FAILED") or 0)
        uncertain = int(counts.get("UNCERTAIN") or 0)
        issues = failed + uncertain
        if issues == 0:
            owner_copy = "No message issues"
        elif failed and uncertain:
            owner_copy = f"{failed} failed, {uncertain} uncertain"
        elif failed:
            owner_copy = f"{failed} failed message{'s' if failed != 1 else ''}"
        else:
            owner_copy = f"{uncertain} uncertain message{'s' if uncertain != 1 else ''}"

        ai_enabled = messages_ai_draft_enabled()
        ai_copy = "AI Draft: Available" if ai_enabled else "AI Draft: Off"
        return {
            "ok": True,
            "unavailable": False,
            "sent_recent": sent,
            "failed": failed,
            "uncertain": uncertain,
            "issues": issues,
            "owner_copy": owner_copy,
            "attention": issues > 0,
            "ai_draft_copy": ai_copy,
            "ai_draft_available": ai_enabled,
            "link": "/messages",
            "link_label": "Open Messages",
            # Explicit contract: never include message bodies
            "private_body_exposed": False,
        }
    except Exception:
        return _panel_error("Messages summary unavailable")


def _scheduler_panel(*, db) -> dict[str, Any]:
    try:
        from sqlalchemy import func
        from src.core.scheduler_models import JobStatus, ScheduledJob

        now = _utcnow().replace(tzinfo=None)
        since = now - timedelta(days=_SCHEDULER_LOOKBACK_DAYS)
        upcoming = (
            db.query(func.count(ScheduledJob.id))
            .filter(
                ScheduledJob.status == JobStatus.PENDING.value,
                ScheduledJob.run_at >= now,
            )
            .scalar()
            or 0
        )
        failed = (
            db.query(func.count(ScheduledJob.id))
            .filter(
                ScheduledJob.status == JobStatus.FAILED.value,
                ScheduledJob.updated_at >= since,
            )
            .scalar()
            or 0
        )
        uncertain = 0
        try:
            uncertain = (
                db.query(func.count(ScheduledJob.id))
                .filter(
                    ScheduledJob.status == JobStatus.UNCERTAIN.value,
                    ScheduledJob.updated_at >= since,
                )
                .scalar()
                or 0
            )
        except Exception:
            uncertain = 0

        dm_enabled = scheduled_dm_enabled()
        if not dm_enabled:
            scheduled_dm_copy = "Scheduled messages: Not enabled yet"
        elif int(upcoming) == 0:
            scheduled_dm_copy = "No upcoming scheduled messages"
        else:
            scheduled_dm_copy = f"{int(upcoming)} upcoming"

        attention = int(failed) > 0 or int(uncertain) > 0
        if int(upcoming) == 0 and int(failed) == 0 and int(uncertain) == 0:
            empty_copy = "No upcoming scheduled messages"
        else:
            empty_copy = None

        return {
            "ok": True,
            "unavailable": False,
            "upcoming": int(upcoming),
            "failed": int(failed),
            "uncertain": int(uncertain),
            "scheduled_dm_enabled": dm_enabled,
            "scheduled_dm_copy": scheduled_dm_copy,
            "empty_state": empty_copy,
            "owner_copy": scheduled_dm_copy,
            "attention": attention,
            "link": "/scheduler",
            "link_label": "Open Scheduler",
        }
    except Exception:
        return _panel_error("Scheduler summary unavailable")


def _stories_panel(*, db) -> dict[str, Any]:
    """Optional cheap DB-only story signal; omit on failure."""
    try:
        from sqlalchemy import func
        from src.core.models import Story

        today_start = _utcnow().replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
        today = (
            db.query(func.count(Story.id))
            .filter(Story.published_at >= today_start)
            .scalar()
            or 0
        )
        last = (
            db.query(Story.published_at)
            .filter(Story.published_at.isnot(None))
            .order_by(Story.published_at.desc())
            .limit(1)
            .scalar()
        )
        last_human = None
        if last is not None:
            last_dt = last if getattr(last, "tzinfo", None) else last.replace(tzinfo=timezone.utc)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            last_human = humanize_age((_utcnow() - last_dt.astimezone(timezone.utc)).total_seconds())
        return {
            "ok": True,
            "unavailable": False,
            "stories_today": int(today),
            "last_published_human": last_human,
            "owner_copy": (
                f"Stories today: {int(today)}"
                + (f" · Last {last_human}" if last_human else "")
            ),
            "attention": False,
            "link": "/stories",
            "link_label": "View Stories",
        }
    except Exception:
        return {
            "ok": False,
            "unavailable": True,
            "omit": True,
            "owner_copy": None,
            "attention": False,
        }


def _backup_panel() -> dict[str, Any]:
    try:
        if not _BACKUP_STATUS_PATH.is_file():
            return {
                "ok": True,
                "unavailable": False,
                "state": "unknown",
                "owner_copy": "Backup status unavailable",
                "attention": True,
                "surface": True,
            }
        data = json.loads(_BACKUP_STATUS_PATH.read_text(encoding="utf-8"))
        state = str(data.get("state") or "")
        off_host = str(data.get("off_host") or "")
        finished = _parse_iso(data.get("finished_at_utc"))
        age_h = None
        if finished is not None:
            age_h = (_utcnow() - finished).total_seconds() / 3600.0
        overdue = age_h is not None and age_h > _BACKUP_MAX_AGE_HOURS
        failed = state != "ok" or (off_host not in {"ok", "dry_run", "disabled"} and off_host != "")
        if failed:
            return {
                "ok": True,
                "state": "failed",
                "owner_copy": "Backup failed",
                "attention": True,
                "surface": True,
            }
        if overdue:
            return {
                "ok": True,
                "state": "overdue",
                "owner_copy": "Backup overdue",
                "attention": True,
                "surface": True,
            }
        return {
            "ok": True,
            "state": "healthy",
            "owner_copy": "Backups healthy",
            "attention": False,
            "surface": False,  # subtle / omit from primary alert strip
            "age_hours": age_h,
        }
    except Exception:
        return {
            "ok": False,
            "state": "unknown",
            "owner_copy": "Backup status unavailable",
            "attention": True,
            "surface": True,
        }


def _disk_panel() -> dict[str, Any]:
    try:
        usage = shutil.disk_usage("/")
        pct = int(round(100.0 * usage.used / usage.total)) if usage.total else 0
        if pct >= _DISK_CRIT_PCT:
            return {
                "ok": True,
                "pct": pct,
                "state": "critical",
                "owner_copy": f"Disk critically low ({pct}% used)",
                "attention": True,
                "surface": True,
            }
        if pct >= _DISK_WARN_PCT:
            return {
                "ok": True,
                "pct": pct,
                "state": "warning",
                "owner_copy": f"Disk space low ({pct}% used)",
                "attention": True,
                "surface": True,
            }
        return {
            "ok": True,
            "pct": pct,
            "state": "healthy",
            "owner_copy": "Disk healthy",
            "attention": False,
            "surface": False,
        }
    except Exception:
        return {
            "ok": False,
            "state": "unknown",
            "owner_copy": "Disk status unavailable",
            "attention": False,
            "surface": False,
        }


def _system_panel(
    *,
    accounts: dict[str, Any],
    messages: dict[str, Any],
    scheduler: dict[str, Any],
    fleet: dict[str, Any],
    backup: dict[str, Any],
    disk: dict[str, Any],
) -> dict[str, Any]:
    reasons: list[str] = []
    if accounts.get("attention"):
        reasons.append(accounts.get("owner_copy") or "Accounts need attention")
    if messages.get("attention"):
        reasons.append(messages.get("owner_copy") or "Message issues")
    if scheduler.get("attention"):
        reasons.append("Scheduler failures need review")
    if fleet.get("attention"):
        reasons.append(fleet.get("owner_copy") or "Fleet health needs refresh")
    if backup.get("attention") and backup.get("surface"):
        reasons.append(backup.get("owner_copy") or "Backup issue")
    if disk.get("attention") and disk.get("surface"):
        reasons.append(disk.get("owner_copy") or "Disk issue")

    if reasons:
        return {
            "ok": True,
            "state": "needs_attention",
            "owner_copy": "System needs attention",
            "detail": reasons[0],
            "attention": True,
        }
    return {
        "ok": True,
        "state": "healthy",
        "owner_copy": "System healthy",
        "detail": None,
        "attention": False,
    }


def build_owner_dashboard_snapshot(*, db=None) -> dict[str, Any]:
    """Build the owner home snapshot. Optional db session; opens one if needed."""
    from src.core.database import get_db_context

    accounts = _accounts_panel()
    fleet = _fleet_panel()
    backup = _backup_panel()
    disk = _disk_panel()

    messages: dict[str, Any]
    scheduler: dict[str, Any]
    stories: dict[str, Any]

    def _with_db(session) -> None:
        nonlocal messages, scheduler, stories
        messages = _messages_panel(db=session)
        scheduler = _scheduler_panel(db=session)
        stories = _stories_panel(db=session)

    try:
        if db is not None:
            _with_db(db)
        else:
            with get_db_context() as session:
                _with_db(session)
    except Exception:
        messages = _panel_error("Messages summary unavailable")
        scheduler = _panel_error("Scheduler summary unavailable")
        stories = {"ok": False, "omit": True, "attention": False}

    system = _system_panel(
        accounts=accounts,
        messages=messages,
        scheduler=scheduler,
        fleet=fleet,
        backup=backup,
        disk=disk,
    )

    return {
        "ok": True,
        "generated_at": _utcnow().isoformat().replace("+00:00", "Z"),
        "system": system,
        "accounts": accounts,
        "messages": messages,
        "scheduler": scheduler,
        "fleet": fleet,
        "stories": stories,
        "backup": backup,
        "disk": disk,
        "contracts": {
            "live_telegram_calls": 0,
            "openai_calls": 0,
            "owner_technical_terms_exposed": False,
            "private_body_exposed": False,
        },
    }
