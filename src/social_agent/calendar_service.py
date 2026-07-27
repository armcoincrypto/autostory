"""Content calendar — schedule queue only. Never publishes live."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from src.social_agent.models import SocialCalendarEntry, SocialContentItem
from src.social_agent.platforms import PLATFORM_LIMITS, normalize_status

CONFLICT_WINDOW_MINUTES = 30


def _parse_dt(value: str | datetime) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        # Accept Zulu and offset forms; store naive UTC.
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except ValueError:
        return None


def _entry_dict(row: SocialCalendarEntry) -> dict[str, Any]:
    return {
        "id": row.id,
        "content_id": row.content_id,
        "platform": row.platform,
        "title": row.title,
        "scheduled_for": row.scheduled_for.isoformat() + "Z" if row.scheduled_for else None,
        "timezone": row.timezone,
        "status": row.status,
        "conflict": bool(row.conflict),
        "notes": row.notes,
        "created_by": row.created_by,
        "live_publish": False,
    }


def _refresh_conflicts(db: Session, *, workspace_id: str, platform: str, around: datetime) -> None:
    window_start = around - timedelta(minutes=CONFLICT_WINDOW_MINUTES)
    window_end = around + timedelta(minutes=CONFLICT_WINDOW_MINUTES)
    rows = (
        db.query(SocialCalendarEntry)
        .filter(
            SocialCalendarEntry.workspace_id == workspace_id,
            SocialCalendarEntry.platform == platform,
            SocialCalendarEntry.status.in_(["queued", "scheduled"]),
            SocialCalendarEntry.scheduled_for >= window_start,
            SocialCalendarEntry.scheduled_for <= window_end,
        )
        .order_by(SocialCalendarEntry.scheduled_for.asc())
        .all()
    )
    for row in rows:
        row.conflict = False
    for i, row in enumerate(rows):
        for other in rows:
            if other.id == row.id:
                continue
            delta = abs((other.scheduled_for - row.scheduled_for).total_seconds())
            if delta <= CONFLICT_WINDOW_MINUTES * 60:
                row.conflict = True
                other.conflict = True


def list_entries(
    db: Session,
    *,
    workspace_id: str = "default",
    view: str = "month",
    anchor: str | None = None,
) -> dict[str, Any]:
    center = _parse_dt(anchor or "") or datetime.utcnow()
    view_l = (view or "month").lower()
    if view_l == "week":
        start = center - timedelta(days=center.weekday())
        end = start + timedelta(days=7)
    elif view_l == "agenda":
        start = center - timedelta(days=1)
        end = center + timedelta(days=21)
    else:
        start = center.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)

    rows = (
        db.query(SocialCalendarEntry)
        .filter(
            SocialCalendarEntry.workspace_id == workspace_id,
            SocialCalendarEntry.scheduled_for >= start,
            SocialCalendarEntry.scheduled_for < end,
        )
        .order_by(SocialCalendarEntry.scheduled_for.asc())
        .all()
    )
    queue = (
        db.query(SocialCalendarEntry)
        .filter(
            SocialCalendarEntry.workspace_id == workspace_id,
            SocialCalendarEntry.status.in_(["queued", "scheduled"]),
            SocialCalendarEntry.scheduled_for >= datetime.utcnow(),
        )
        .order_by(SocialCalendarEntry.scheduled_for.asc())
        .limit(50)
        .all()
    )
    return {
        "ok": True,
        "view": view_l,
        "range": {"start": start.isoformat() + "Z", "end": end.isoformat() + "Z"},
        "entries": [_entry_dict(r) for r in rows],
        "scheduled_queue": [_entry_dict(r) for r in queue],
        "live_publish_enabled": False,
        "message": "Calendar stores a schedule queue only. Live publishing remains disabled.",
    }


def create_entry(
    db: Session,
    *,
    actor: str | None,
    content_id: int,
    platform: str,
    scheduled_for: str | datetime,
    timezone_name: str = "UTC",
    notes: str | None = None,
    workspace_id: str = "default",
) -> dict[str, Any]:
    item = db.query(SocialContentItem).filter(SocialContentItem.id == int(content_id)).first()
    if not item:
        return {"ok": False, "error": "content_not_found"}
    status = normalize_status(item.status)
    if status not in {"APPROVED", "NEEDS_REVIEW", "DRAFT"}:
        return {"ok": False, "error": "content_not_schedulable", "status": status}
    plat = (platform or "").strip().lower()
    if plat not in PLATFORM_LIMITS:
        return {"ok": False, "error": "unsupported_platform", "platform": plat}
    when = _parse_dt(scheduled_for)
    if when is None:
        return {"ok": False, "error": "invalid_scheduled_for"}
    row = SocialCalendarEntry(
        workspace_id=workspace_id,
        content_id=item.id,
        platform=plat,
        title=(item.title or f"Content #{item.id}")[:255],
        scheduled_for=when,
        timezone=(timezone_name or "UTC")[:64],
        status="queued",
        notes=(notes or "")[:2000] or None,
        created_by=actor,
    )
    db.add(row)
    db.flush()
    _refresh_conflicts(db, workspace_id=workspace_id, platform=plat, around=when)
    db.flush()
    return {
        "ok": True,
        "entry": _entry_dict(row),
        "live_publish_enabled": False,
        "message": "Queued for calendar only — scheduler mutations remain locked.",
    }


def move_entry(
    db: Session,
    *,
    actor: str | None,
    entry_id: int,
    scheduled_for: str | datetime,
    workspace_id: str = "default",
) -> dict[str, Any]:
    row = (
        db.query(SocialCalendarEntry)
        .filter(SocialCalendarEntry.id == int(entry_id), SocialCalendarEntry.workspace_id == workspace_id)
        .first()
    )
    if not row:
        return {"ok": False, "error": "not_found"}
    when = _parse_dt(scheduled_for)
    if when is None:
        return {"ok": False, "error": "invalid_scheduled_for"}
    row.scheduled_for = when
    row.updated_at = datetime.utcnow()
    db.flush()
    _refresh_conflicts(db, workspace_id=workspace_id, platform=row.platform, around=when)
    db.flush()
    return {"ok": True, "entry": _entry_dict(row), "actor": actor}


def cancel_entry(db: Session, *, entry_id: int, workspace_id: str = "default") -> dict[str, Any]:
    row = (
        db.query(SocialCalendarEntry)
        .filter(SocialCalendarEntry.id == int(entry_id), SocialCalendarEntry.workspace_id == workspace_id)
        .first()
    )
    if not row:
        return {"ok": False, "error": "not_found"}
    row.status = "cancelled"
    row.conflict = False
    row.updated_at = datetime.utcnow()
    db.flush()
    return {"ok": True, "entry": _entry_dict(row)}
