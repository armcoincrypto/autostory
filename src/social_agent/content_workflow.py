"""Content Studio workflow — update variants, previews, approval transitions.

Reuses SocialContentItem / SocialContentVariant. Never publishes.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from src.social_agent.models import SocialContentItem, SocialContentStatusEvent, SocialContentVariant
from src.social_agent.platforms import (
    CONTENT_STATUSES,
    DEFAULT_VARIANT_PLATFORMS,
    PLATFORM_LIMITS,
    STATUS_TRANSITIONS,
    normalize_status,
    platform_catalog,
)
from src.social_agent.publishing.content import parse_content
from src.social_agent.publishing.renderers.preview import render_studio_preview


def _validate_variant(platform: str, body: str) -> dict[str, Any]:
    limit = PLATFORM_LIMITS.get(platform, 5000)
    ok = 0 < len(body) <= limit
    return {
        "ok": ok,
        "char_count": len(body),
        "limit": limit,
        "errors": [] if ok else (["empty"] if not body else ["over_limit"]),
    }


def update_variant(
    db: Session,
    *,
    actor: str | None,
    content_id: int,
    variant_id: int,
    body: str,
) -> dict[str, Any]:
    item = db.query(SocialContentItem).filter(SocialContentItem.id == int(content_id)).first()
    if not item:
        return {"ok": False, "error": "not_found"}
    status = normalize_status(item.status)
    if status in {"PUBLISHED", "ARCHIVED"}:
        return {"ok": False, "error": "content_locked", "status": status}
    row = (
        db.query(SocialContentVariant)
        .filter(
            SocialContentVariant.id == int(variant_id),
            SocialContentVariant.content_id == int(content_id),
        )
        .first()
    )
    if not row:
        return {"ok": False, "error": "variant_not_found"}
    text = (body or "").strip()
    row.body = text
    row.char_count = len(text)
    row.validation_json = json.dumps(_validate_variant(row.platform, text))
    item.updated_at = datetime.utcnow()
    db.flush()
    return {
        "ok": True,
        "variant": {
            "id": row.id,
            "platform": row.platform,
            "language": row.language,
            "body": row.body,
            "char_count": row.char_count,
            "validation": json.loads(row.validation_json or "{}"),
        },
        "actor": actor,
    }


def upsert_variant(
    db: Session,
    *,
    actor: str | None,
    content_id: int,
    platform: str,
    language: str,
    body: str,
) -> dict[str, Any]:
    item = db.query(SocialContentItem).filter(SocialContentItem.id == int(content_id)).first()
    if not item:
        return {"ok": False, "error": "not_found"}
    plat = (platform or "").strip().lower()
    lang = (language or "EN").strip().upper() or "EN"
    if plat not in PLATFORM_LIMITS:
        return {"ok": False, "error": "unsupported_platform", "platform": plat}
    row = (
        db.query(SocialContentVariant)
        .filter(
            SocialContentVariant.content_id == int(content_id),
            SocialContentVariant.platform == plat,
            SocialContentVariant.language == lang,
        )
        .first()
    )
    text = (body or "").strip()
    if row is None:
        row = SocialContentVariant(
            content_id=int(content_id),
            platform=plat,
            language=lang,
            body=text,
            char_count=len(text),
            validation_json=json.dumps(_validate_variant(plat, text)),
        )
        db.add(row)
    else:
        row.body = text
        row.char_count = len(text)
        row.validation_json = json.dumps(_validate_variant(plat, text))
    item.updated_at = datetime.utcnow()
    db.flush()
    return {
        "ok": True,
        "variant": {
            "id": row.id,
            "platform": row.platform,
            "language": row.language,
            "body": row.body,
            "char_count": row.char_count,
            "validation": json.loads(row.validation_json or "{}"),
        },
        "actor": actor,
    }


def transition_status(
    db: Session,
    *,
    actor: str | None,
    content_id: int,
    to_status: str,
    note: str | None = None,
) -> dict[str, Any]:
    item = db.query(SocialContentItem).filter(SocialContentItem.id == int(content_id)).first()
    if not item:
        return {"ok": False, "error": "not_found"}
    current = normalize_status(item.status)
    # Persist normalized form if legacy alias present.
    if item.status == "READY_FOR_REVIEW":
        item.status = "NEEDS_REVIEW"
        current = "NEEDS_REVIEW"
    target = normalize_status(to_status)
    if target not in CONTENT_STATUSES:
        return {"ok": False, "error": "invalid_status", "status": target}
    allowed = STATUS_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        return {
            "ok": False,
            "error": "transition_denied",
            "from": current,
            "to": target,
            "allowed": sorted(allowed),
        }
    # PUBLISHED is reserved for future certified publish — deny auto-mark without explicit ops path.
    if target == "PUBLISHED":
        return {
            "ok": False,
            "error": "publish_status_locked",
            "message": "PUBLISHED status requires a certified provider publish path. Use dry-run only.",
        }
    event = SocialContentStatusEvent(
        content_id=item.id,
        from_status=current,
        to_status=target,
        actor=actor,
        note=(note or "")[:2000] or None,
    )
    db.add(event)
    item.status = target
    item.updated_at = datetime.utcnow()
    db.flush()
    return {
        "ok": True,
        "content_id": item.id,
        "from_status": current,
        "to_status": target,
        "event_id": event.id,
    }


def list_status_events(db: Session, content_id: int) -> dict[str, Any]:
    rows = (
        db.query(SocialContentStatusEvent)
        .filter(SocialContentStatusEvent.content_id == int(content_id))
        .order_by(SocialContentStatusEvent.id.asc())
        .all()
    )
    return {
        "ok": True,
        "events": [
            {
                "id": r.id,
                "from_status": r.from_status,
                "to_status": r.to_status,
                "actor": r.actor,
                "note": r.note,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }


def studio_previews(
    db: Session,
    *,
    content_id: int,
    connection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Render platform previews via canonical preview renderers (no provider HTTP)."""
    item = db.query(SocialContentItem).filter(SocialContentItem.id == int(content_id)).first()
    if not item:
        return {"ok": False, "error": "not_found"}
    variants = (
        db.query(SocialContentVariant)
        .filter(SocialContentVariant.content_id == item.id)
        .order_by(SocialContentVariant.id.asc())
        .all()
    )
    by_platform: dict[str, SocialContentVariant] = {}
    for v in variants:
        # Prefer EN when multiple languages exist.
        key = v.platform
        if key not in by_platform or v.language.upper() == "EN":
            by_platform[key] = v

    previews = []
    for plat in platform_catalog():
        pid = plat["id"]
        variant = by_platform.get(pid)
        body = variant.body if variant else (item.brief or item.title or "")
        content = parse_content({"text": body, "language": (variant.language if variant else "EN")})
        rendered = render_studio_preview(pid, content, connection=connection)
        previews.append(
            {
                "platform": pid,
                "label": plat["label"],
                "char_limit": plat["char_limit"],
                "char_count": len(body),
                "variant_id": variant.id if variant else None,
                "body": body,
                "validation": _validate_variant(pid, body),
                "preview": rendered,
            }
        )
    return {
        "ok": True,
        "content_id": item.id,
        "status": normalize_status(item.status),
        "title": item.title,
        "platforms": DEFAULT_VARIANT_PLATFORMS,
        "previews": previews,
        "provider_called": False,
    }
