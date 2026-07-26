"""Canonical publishing dry-run service — shared by UI and AI Assistant."""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy.orm import Session

from src.social_agent.models import SocialConnection, SocialContentItem, SocialContentVariant, SocialPublishDryRun
from src.social_agent.publishing.content import PublishContent, parse_content
from src.social_agent.publishing.renderers.meta import get_renderer
from src.social_agent.publishing.validate import validate_request


def _audit(
    db: Session,
    *,
    actor: str | None,
    action: str,
    tool_name: str | None = None,
    decision: str | None = None,
    dry_run: bool = True,
    detail: dict[str, Any] | None = None,
) -> None:
    # Local import avoids circular import with services.py
    from src.social_agent.services import _audit as shared_audit

    shared_audit(
        db,
        actor=actor,
        action=action,
        tool_name=tool_name,
        decision=decision,
        dry_run=dry_run,
        detail=detail,
    )


def _payload_hash(payloads: list[dict[str, Any]]) -> str:
    canonical = json.dumps(payloads, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _safe_connection(db: Session, *, workspace_id: str = "default") -> dict[str, Any] | None:
    row = (
        db.query(SocialConnection)
        .filter(SocialConnection.provider == "meta", SocialConnection.workspace_id == workspace_id)
        .order_by(SocialConnection.id.desc())
        .first()
    )
    if not row:
        return None
    dests = []
    try:
        dests = json.loads(row.destinations_json or "[]")
    except json.JSONDecodeError:
        dests = []
    selected = next((d for d in dests if str(d.get("page_id")) == str(row.selected_page_id)), None) if row.selected_page_id else None
    ig_username = None
    if selected:
        ig_username = selected.get("instagram_username")
        if not ig_username and isinstance(selected.get("instagram_business_account"), dict):
            ig_username = selected["instagram_business_account"].get("username")
    return {
        "id": row.id,
        "provider": row.provider,
        "status": row.status,
        "health": row.health,
        "display_name": row.display_name,
        "selected_page_id": row.selected_page_id,
        "selected_page_name": (selected or {}).get("page_name") if selected else row.display_name,
        "selected_instagram_id": row.selected_instagram_id,
        "selected_instagram_username": ig_username,
        # Never expose credentials.
    }


def _content_from_draft(db: Session, content_id: int, base: PublishContent) -> PublishContent:
    item = db.query(SocialContentItem).filter(SocialContentItem.id == int(content_id)).first()
    if not item:
        return base
    variants = (
        db.query(SocialContentVariant)
        .filter(SocialContentVariant.content_id == int(content_id))
        .order_by(SocialContentVariant.id.asc())
        .all()
    )
    preferred = None
    for v in variants:
        if v.platform in {"facebook", "instagram", "meta"} and (v.language or "").upper() == (base.language or "EN").upper():
            preferred = v
            break
    if preferred is None and variants:
        preferred = variants[0]
    text = base.text or ((preferred.body if preferred else None) or item.brief or "")
    return PublishContent(
        text=text,
        hashtags=base.hashtags,
        images=base.images,
        videos=base.videos,
        link=base.link,
        cta=base.cta,
        language=base.language,
        brand_voice=base.brand_voice,
        title=base.title or item.title,
        content_id=item.id,
    )


def run_publishing_dry_run(
    db: Session,
    *,
    actor: str | None,
    destinations: list[str],
    content: dict[str, Any] | None = None,
    content_id: int | None = None,
    workspace_id: str = "default",
) -> dict[str, Any]:
    """Build validation + provider payload previews. Never calls Meta Graph."""
    parsed = parse_content(content)
    if content_id is not None:
        parsed.content_id = int(content_id)
    if parsed.content_id is not None:
        parsed = _content_from_draft(db, parsed.content_id, parsed)

    dests = [str(d).strip().lower() for d in (destinations or []) if str(d).strip()]
    # Normalize aliases from older UI
    alias = {
        "facebook": "facebook_page",
        "fb": "facebook_page",
        "instagram": "instagram_feed",
        "ig": "instagram_feed",
        "ig_feed": "instagram_feed",
        "ig_carousel": "instagram_carousel",
        "ig_story": "instagram_story",
        "instagram_stories": "instagram_story",
    }
    dests = [alias.get(d, d) for d in dests]

    validation = validate_request(destinations=dests, content=parsed)
    connection = _safe_connection(db, workspace_id=workspace_id)
    payloads: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for dest_report in validation["destinations"]:
        warnings.extend(dest_report.get("warnings") or [])
        dest = dest_report.get("destination")
        if not dest_report.get("ok"):
            continue
        renderer = get_renderer(dest)
        if renderer is None:
            continue
        payloads.append(renderer.render(parsed, connection=connection))

    ready = bool(validation["ok"]) and bool(payloads) and all(p.get("provider_called") is False for p in payloads)
    status = "READY" if ready else ("VALIDATION_FAILED" if not validation["ok"] else "BLOCKED")
    if ready and connection and not connection.get("selected_page_id"):
        # Still allow payload preview, but mark blocked for operator readiness.
        status = "READY_NO_CONNECTION"
        warnings.append(
            {
                "code": "meta_page_not_selected",
                "message": "Meta connection has no selected Facebook Page; payloads use placeholders.",
            }
        )

    digest = _payload_hash(payloads) if payloads else hashlib.sha256(b"").hexdigest()
    row = SocialPublishDryRun(
        workspace_id=workspace_id,
        actor=actor,
        content_id=parsed.content_id,
        destinations_json=json.dumps(dests),
        content_snapshot_json=json.dumps(parsed.to_dict(), default=str)[:20000],
        validation_json=json.dumps(validation, default=str)[:20000],
        payloads_json=json.dumps(payloads, default=str)[:50000],
        warnings_json=json.dumps(warnings, default=str)[:8000],
        payload_hash=digest,
        status=status,
        provider_called=False,
        provider_http_posts=0,
        idempotency_key=f"dryrun:{uuid.uuid4().hex}",
    )
    db.add(row)
    db.flush()

    _audit(
        db,
        actor=actor,
        action="publishing.dry_run",
        tool_name="publishing.dry_run",
        decision="dry_run",
        dry_run=True,
        detail={
            "dry_run_id": row.id,
            "workspace_id": workspace_id,
            "provider": "meta",
            "destinations": dests,
            "payload_hash": digest,
            "validation_ok": validation["ok"],
            "status": status,
            "provider_called": False,
            "provider_http_posts": 0,
        },
    )

    return {
        "ok": True,
        "dry_run": True,
        "dry_run_id": row.id,
        "status": status,
        "ready_for_publishing": status.startswith("READY"),
        "published": False,
        "provider_called": False,
        "provider_http_posts": 0,
        "meta_provider_mutations": 0,
        "payload_hash": digest,
        "idempotency_key": row.idempotency_key,
        "content": parsed.to_dict(),
        "connection": connection,
        "validation": validation,
        "warnings": warnings,
        "payloads": payloads,
        "workflow": [
            "draft",
            "validate",
            "render_platform_payload",
            "dry_run_preview",
            "provider_validation",
            "result",
        ],
        "message": (
            "Dry-run complete. Payloads were built but not sent. Live publishing remains disabled."
            if ready
            else "Dry-run completed with validation issues. Nothing was published."
        ),
    }


def list_dry_run_history(
    db: Session,
    *,
    workspace_id: str = "default",
    limit: int = 50,
) -> dict[str, Any]:
    rows = (
        db.query(SocialPublishDryRun)
        .filter(SocialPublishDryRun.workspace_id == workspace_id)
        .order_by(SocialPublishDryRun.id.desc())
        .limit(max(1, min(int(limit), 200)))
        .all()
    )
    return {
        "ok": True,
        "items": [
            {
                "id": r.id,
                "status": r.status,
                "content_id": r.content_id,
                "destinations": json.loads(r.destinations_json or "[]"),
                "payload_hash": r.payload_hash,
                "provider_called": bool(r.provider_called),
                "provider_http_posts": int(r.provider_http_posts or 0),
                "actor": r.actor,
                "created_at": r.created_at.isoformat() + "Z" if r.created_at else None,
            }
            for r in rows
        ],
    }


def get_dry_run_preview(db: Session, dry_run_id: int, *, workspace_id: str = "default") -> dict[str, Any]:
    row = (
        db.query(SocialPublishDryRun)
        .filter(SocialPublishDryRun.id == int(dry_run_id), SocialPublishDryRun.workspace_id == workspace_id)
        .first()
    )
    if not row:
        return {"ok": False, "error": "not_found"}
    return {
        "ok": True,
        "dry_run": True,
        "dry_run_id": row.id,
        "status": row.status,
        "ready_for_publishing": str(row.status or "").startswith("READY"),
        "published": False,
        "provider_called": bool(row.provider_called),
        "provider_http_posts": int(row.provider_http_posts or 0),
        "meta_provider_mutations": 0,
        "payload_hash": row.payload_hash,
        "idempotency_key": row.idempotency_key,
        "actor": row.actor,
        "content_id": row.content_id,
        "destinations": json.loads(row.destinations_json or "[]"),
        "content": json.loads(row.content_snapshot_json or "{}"),
        "validation": json.loads(row.validation_json or "{}"),
        "warnings": json.loads(row.warnings_json or "[]"),
        "payloads": json.loads(row.payloads_json or "[]"),
        "created_at": row.created_at.isoformat() + "Z" if row.created_at else None,
    }
