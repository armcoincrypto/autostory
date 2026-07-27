"""Canonical Social Agent application services (UI and AI share these)."""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy.orm import Session

from src.social_agent.integrations import integration_matrix
from src.social_agent.models import (
    SocialAgentAuditEvent,
    SocialAgentConversation,
    SocialAgentMessage,
    SocialConnection,
    SocialContentItem,
    SocialContentVariant,
)
from src.social_agent.permissions import require_permission
from src.social_agent.platforms import (
    DEFAULT_VARIANT_PLATFORMS,
    PLATFORM_LIMITS,
    normalize_status,
)
from src.social_agent.tools import get_tool, list_tools

logger = structlog.get_logger(__name__)


def _audit(
    db: Session,
    *,
    actor: str | None,
    action: str,
    tool_name: str | None = None,
    decision: str | None = None,
    dry_run: bool = False,
    detail: dict[str, Any] | None = None,
) -> None:
    db.add(
        SocialAgentAuditEvent(
            actor=actor,
            action=action,
            tool_name=tool_name,
            decision=decision,
            dry_run=dry_run,
            detail_json=json.dumps(detail or {}, default=str)[:8000],
        )
    )


def overview(db: Session) -> dict[str, Any]:
    connections = db.query(SocialConnection).order_by(SocialConnection.id.desc()).limit(20).all()
    drafts = (
        db.query(SocialContentItem)
        .filter(SocialContentItem.status.in_(["DRAFT", "READY_FOR_REVIEW", "NEEDS_REVIEW", "APPROVED"]))
        .order_by(SocialContentItem.id.desc())
        .limit(10)
        .all()
    )
    recent_msgs = db.query(SocialAgentMessage).order_by(SocialAgentMessage.id.desc()).limit(5).all()
    integ = integration_matrix()
    meta_sum = _meta_connection_summary(connections)
    from src.social_agent.calendar_service import list_entries as calendar_list

    cal = calendar_list(db, view="agenda")
    return {
        "ok": True,
        "connections": [
            {
                "id": c.id,
                "provider": c.provider,
                "display_name": c.display_name,
                "status": c.status,
                "health": c.health,
            }
            for c in connections
        ],
        "meta_connection_summary": meta_sum,
        "drafts_awaiting_review": [
            {"id": d.id, "title": d.title, "status": normalize_status(d.status)} for d in drafts
        ],
        "scheduled_posts": cal.get("scheduled_queue") or [],
        "publishing_failures": [],
        "recent_successful_posts": [],
        "upcoming_calendar": cal.get("entries") or [],
        "recent_ai_activity": [
            {"id": m.id, "role": m.role, "preview": (m.content or "")[:120]} for m in recent_msgs
        ],
        "integrations": integ,
        "alerts": _build_alerts(integ, connections),
        "onboarding": _onboarding(integ, connections),
        "live_publish_enabled": False,
    }


def _meta_connection_summary(connections: list[SocialConnection]) -> dict[str, Any]:
    meta = next((c for c in connections if c.provider == "meta"), None)
    if not meta:
        return {
            "connected": False,
            "health": None,
            "page_healthy": False,
            "instagram_healthy": False,
            "summary": "Meta not connected",
        }
    health = (meta.health or "").upper()
    connected = meta.status == "connected" or health == "CONNECTED"
    page_healthy = health == "CONNECTED"
    instagram_healthy = page_healthy and bool(meta.selected_instagram_id)
    if health == "INSTAGRAM_UNAVAILABLE":
        summary = "Meta connected · Facebook Page healthy · Instagram needs attention"
    elif page_healthy and instagram_healthy:
        summary = "Meta connected · Facebook Page healthy · Instagram account healthy"
    elif page_healthy:
        summary = "Meta connected · Facebook Page healthy · Instagram not selected"
    elif connected:
        summary = f"Meta connected · health {meta.health or meta.status}"
    else:
        summary = f"Meta status {meta.status} · health {meta.health or 'unknown'}"
    return {
        "connected": connected,
        "health": meta.health,
        "page_healthy": page_healthy,
        "instagram_healthy": instagram_healthy,
        "summary": summary,
    }


def _build_alerts(integ: dict[str, Any], connections: list[SocialConnection]) -> list[str]:
    alerts: list[str] = []
    meta_sum = _meta_connection_summary(connections)
    if integ["meta"]["status"] == "CREDENTIALS_MISSING":
        alerts.append("Meta credentials not configured.")
    elif not meta_sum["connected"]:
        alerts.append("Meta not connected.")
    else:
        alerts.append(meta_sum["summary"])
        if (meta_sum.get("health") or "").upper() == "INSTAGRAM_UNAVAILABLE":
            alerts.append("Instagram account is no longer linked to the selected Facebook Page.")
    if not connections and integ["meta"]["status"] != "CREDENTIALS_MISSING":
        alerts.append("No social connections yet.")
    if not integ["telegram"].get("story_mutations_enabled"):
        alerts.append("Telegram Story mutations fail-closed (expected).")
    return alerts


def _onboarding(integ: dict[str, Any], connections: list[SocialConnection]) -> list[str]:
    steps: list[str] = []
    meta_sum = _meta_connection_summary(connections)
    if integ["meta"]["status"] == "CREDENTIALS_MISSING":
        steps.append("Install Meta App ID/Secret on the server, then connect Meta from Social Accounts.")
    elif not meta_sum["connected"]:
        steps.append("Connect Meta to discover the Exswaping Facebook Page and linked Instagram Professional account.")
    else:
        steps.append(meta_sum["summary"] + ".")
    steps.append("Telegram status is available via AutoStory adapter; live Story publish stays locked.")
    return steps

def create_conversation(db: Session, *, actor: str | None, title: str | None = None) -> dict[str, Any]:
    row = SocialAgentConversation(title=(title or "New chat").strip()[:255], created_by=actor)
    db.add(row)
    db.flush()
    _audit(db, actor=actor, action="conversation.create", detail={"conversation_id": row.id})
    return {"ok": True, "conversation_id": row.id, "title": row.title}


def list_conversations(db: Session, *, limit: int = 30) -> dict[str, Any]:
    rows = (
        db.query(SocialAgentConversation)
        .order_by(SocialAgentConversation.updated_at.desc())
        .limit(max(1, min(limit, 100)))
        .all()
    )
    return {
        "ok": True,
        "conversations": [
            {
                "id": r.id,
                "title": r.title,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ],
    }


def list_messages(db: Session, conversation_id: int) -> dict[str, Any]:
    rows = (
        db.query(SocialAgentMessage)
        .filter(SocialAgentMessage.conversation_id == int(conversation_id))
        .order_by(SocialAgentMessage.id.asc())
        .all()
    )
    return {
        "ok": True,
        "messages": [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "tool_name": m.tool_name,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in rows
        ],
    }


def append_message(
    db: Session,
    *,
    conversation_id: int,
    role: str,
    content: str,
    tool_name: str | None = None,
    tool_payload: dict[str, Any] | None = None,
) -> SocialAgentMessage:
    msg = SocialAgentMessage(
        conversation_id=int(conversation_id),
        role=role,
        content=content or "",
        tool_name=tool_name,
        tool_payload_json=json.dumps(tool_payload or {}, default=str) if tool_payload else None,
    )
    db.add(msg)
    conv = db.query(SocialAgentConversation).filter(SocialAgentConversation.id == int(conversation_id)).first()
    if conv:
        conv.updated_at = datetime.utcnow()
    db.flush()
    return msg


def create_draft(
    db: Session,
    *,
    actor: str | None,
    title: str,
    brief: str | None,
    body: str | None,
    platforms: list[str],
    languages: list[str] | None = None,
) -> dict[str, Any]:
    langs = languages if languages is not None else ["EN"]
    plats = platforms if platforms is not None else ["telegram"]
    item = SocialContentItem(
        title=(title or brief or "Untitled draft").strip()[:255],
        status="DRAFT",
        brief=brief,
        created_by=actor,
    )
    db.add(item)
    db.flush()
    variants = []
    text = (body or brief or "").strip()
    for platform in plats:
        for lang in langs:
            v = SocialContentVariant(
                content_id=item.id,
                platform=platform.lower(),
                language=lang.upper(),
                body=text,
                char_count=len(text),
                validation_json=json.dumps(_validate_variant(platform.lower(), text)),
            )
            db.add(v)
            variants.append(v)
    db.flush()
    _audit(
        db,
        actor=actor,
        action="content.create_draft",
        tool_name="content.create_draft",
        decision="created",
        detail={"content_id": item.id, "variants": len(variants)},
    )
    return {
        "ok": True,
        "content_id": item.id,
        "status": item.status,
        "variants": [
            {
                "id": v.id,
                "platform": v.platform,
                "language": v.language,
                "body": v.body,
                "char_count": v.char_count,
                "validation": json.loads(v.validation_json or "{}"),
            }
            for v in variants
        ],
    }


def generate_variants_local(brief: str, platforms: list[str], languages: list[str]) -> dict[str, Any]:
    """Deterministic local generator used when AI provider is unset."""
    brief = (brief or "").strip()
    plats = [p.strip().lower() for p in (platforms or list(DEFAULT_VARIANT_PLATFORMS)) if p]
    if not plats:
        plats = list(DEFAULT_VARIANT_PLATFORMS)
    out = []
    for platform in plats:
        for lang in languages:
            prefix = {
                "EN": "",
                "RU": "[RU] ",
                "HY": "[HY] ",
            }.get(lang.upper(), f"[{lang}] ")
            # Light platform-aware shaping without a second renderer stack.
            if platform == "tiktok":
                body = f"{prefix}{brief}\n\n#Exswaping".strip()
            elif platform == "youtube_community":
                body = f"{prefix}Community note: {brief}".strip()
            elif platform == "discord":
                body = f"{prefix}**Exswaping** — {brief}".strip()
            elif platform == "linkedin":
                body = f"{prefix}{brief}\n\n— Exswaping".strip()
            else:
                body = f"{prefix}{brief}".strip()
            limit = PLATFORM_LIMITS.get(platform, 5000)
            if len(body) > limit:
                body = body[: max(0, limit - 3)].rstrip() + "..."
            out.append(
                {
                    "platform": platform,
                    "language": lang.upper(),
                    "body": body,
                    "char_count": len(body),
                    "validation": _validate_variant(platform, body),
                    "generator": "local_template",
                }
            )
    return {"ok": True, "variants": out, "ai_used": False}


def _validate_variant(platform: str, body: str) -> dict[str, Any]:
    limit = PLATFORM_LIMITS.get(platform, 5000)
    ok = 0 < len(body) <= limit
    return {
        "ok": ok,
        "char_count": len(body),
        "limit": limit,
        "errors": [] if ok else (["empty"] if not body else ["over_limit"]),
    }


def list_drafts(db: Session, *, limit: int = 50) -> dict[str, Any]:
    rows = (
        db.query(SocialContentItem)
        .order_by(SocialContentItem.id.desc())
        .limit(max(1, min(limit, 200)))
        .all()
    )
    return {
        "ok": True,
        "items": [
            {
                "id": r.id,
                "title": r.title,
                "status": r.status,
                "brief": r.brief,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ],
    }


def global_search(db: Session, *, query: str, limit: int = 20) -> dict[str, Any]:
    """Read-only workspace search across drafts, brand, media, calendar, and tools."""
    q = (query or "").strip().lower()
    limit = max(1, min(int(limit or 20), 50))
    results: list[dict[str, Any]] = []
    if not q:
        return {"ok": True, "query": "", "results": [], "message": "Enter a search query."}

    # Drafts / content
    for row in db.query(SocialContentItem).order_by(SocialContentItem.id.desc()).limit(200).all():
        blob = f"{row.id} {row.title or ''} {row.brief or ''} {row.status or ''}".lower()
        if q in blob:
            results.append(
                {
                    "type": "content",
                    "id": row.id,
                    "title": row.title or f"Draft #{row.id}",
                    "subtitle": normalize_status(row.status),
                    "href": f"/social-agent/content?open={row.id}",
                }
            )
        if len(results) >= limit:
            break

    # Brand knowledge (reuse store)
    from src.social_agent import brand_store as brand

    for hit in (brand.search_knowledge(db, query=q).get("hits") or [])[: max(0, limit - len(results))]:
        results.append(
            {
                "type": "brand",
                "id": hit.get("id"),
                "title": hit.get("title") or hit.get("key"),
                "subtitle": hit.get("category"),
                "href": "/social-agent/brand",
            }
        )

    # Media filenames
    from src.social_agent.models import SocialMediaAsset

    if len(results) < limit:
        for asset in (
            db.query(SocialMediaAsset)
            .filter(SocialMediaAsset.deleted_at.is_(None))
            .order_by(SocialMediaAsset.id.desc())
            .limit(100)
            .all()
        ):
            blob = f"{asset.original_filename} {asset.filename} {asset.mime_type} {asset.kind}".lower()
            if q in blob:
                results.append(
                    {
                        "type": "media",
                        "id": asset.id,
                        "title": asset.original_filename,
                        "subtitle": asset.kind,
                        "href": "/social-agent/media",
                    }
                )
            if len(results) >= limit:
                break

    # Calendar titles
    from src.social_agent.models import SocialCalendarEntry

    if len(results) < limit:
        for entry in db.query(SocialCalendarEntry).order_by(SocialCalendarEntry.id.desc()).limit(100).all():
            blob = f"{entry.title} {entry.platform} {entry.status}".lower()
            if q in blob:
                results.append(
                    {
                        "type": "calendar",
                        "id": entry.id,
                        "title": entry.title,
                        "subtitle": f"{entry.platform} · {entry.status}",
                        "href": "/social-agent/calendar",
                    }
                )
            if len(results) >= limit:
                break

    # Tools / navigation labels
    nav_hits = [
        ("overview", "Overview", "/social-agent"),
        ("assistant", "AI Assistant", "/social-agent/assistant"),
        ("content", "Content Studio", "/social-agent/content"),
        ("publishing", "Publishing Preview", "/social-agent/publishing"),
        ("calendar", "Calendar", "/social-agent/calendar"),
        ("media", "Media Library", "/social-agent/media"),
        ("accounts", "Social Accounts", "/social-agent/accounts"),
        ("analytics", "Analytics", "/social-agent/analytics"),
        ("brand", "Brand Knowledge", "/social-agent/brand"),
        ("settings", "Settings", "/social-agent/settings"),
    ]
    for key, title, href in nav_hits:
        if len(results) >= limit:
            break
        if q in key or q in title.lower():
            results.append({"type": "page", "id": key, "title": title, "subtitle": "Navigate", "href": href})

    for tool in list_tools():
        if len(results) >= limit:
            break
        name = tool.get("name") or ""
        if q in name.lower() or q in (tool.get("description") or "").lower():
            results.append(
                {
                    "type": "tool",
                    "id": name,
                    "title": name,
                    "subtitle": "available" if tool.get("available") else "unavailable",
                    "href": "/social-agent/settings",
                }
            )

    return {"ok": True, "query": query, "results": results[:limit], "provider_called": False}


def get_draft(db: Session, content_id: int) -> dict[str, Any]:
    item = db.query(SocialContentItem).filter(SocialContentItem.id == int(content_id)).first()
    if not item:
        return {"ok": False, "error": "not_found"}
    variants = (
        db.query(SocialContentVariant)
        .filter(SocialContentVariant.content_id == item.id)
        .order_by(SocialContentVariant.id.asc())
        .all()
    )
    return {
        "ok": True,
        "item": {
            "id": item.id,
            "title": item.title,
            "status": normalize_status(item.status),
            "brief": item.brief,
        },
        "variants": [
            {
                "id": v.id,
                "platform": v.platform,
                "language": v.language,
                "body": v.body,
                "char_count": v.char_count,
                "validation": json.loads(v.validation_json or "{}"),
            }
            for v in variants
        ],
    }


def publish_preview(
    db: Session,
    *,
    actor: str | None,
    content_id: int,
    destinations: list[str],
    content: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Backward-compatible alias → canonical publishing dry-run (never publishes)."""
    from src.social_agent.publishing.service import run_publishing_dry_run

    draft = get_draft(db, content_id)
    if not draft.get("ok"):
        return draft
    # Telegram and other non-Meta destinations stay fail-closed and are not rendered.
    meta_dests = []
    skipped = []
    for dest in destinations or []:
        d = str(dest).strip().lower()
        if d in {"telegram", "x", "linkedin", "discord", "youtube", "tiktok"}:
            skipped.append(
                {
                    "destination": d,
                    "ready": False,
                    "reason": "provider_not_in_dry_run_pipeline",
                }
            )
            continue
        meta_dests.append(d)
    if not meta_dests:
        # Preserve previous fail-closed preview shape when only non-Meta dests requested.
        idem = f"preview:{content_id}:{uuid.uuid4().hex[:12]}"
        _audit(
            db,
            actor=actor,
            action="publishing.preview",
            tool_name="publishing.preview",
            decision="dry_run",
            dry_run=True,
            detail={"content_id": content_id, "destinations": skipped, "idempotency_key": idem},
        )
        return {
            "ok": True,
            "dry_run": True,
            "published": False,
            "idempotency_key": idem,
            "content": draft["item"],
            "variants": draft["variants"],
            "destinations": skipped,
            "provider_called": False,
            "provider_http_posts": 0,
            "meta_provider_mutations": 0,
            "external_ids": {},
            "message": "No Meta destinations selected. Live publishing remains disabled.",
        }
    out = run_publishing_dry_run(
        db,
        actor=actor,
        destinations=meta_dests,
        content=content,
        content_id=int(content_id),
    )
    out["content_item"] = draft["item"]
    out["variants"] = draft["variants"]
    out["skipped_destinations"] = skipped
    # Legacy key used by Content Studio UI.
    out["destinations"] = [
        {
            "destination": p.get("destination"),
            "ready": out.get("ready_for_publishing") is True and bool(p.get("would_send") is False),
            "reason": "dry_run_payload_ready" if out.get("validation", {}).get("ok") else "validation_failed",
            "payload_preview": True,
        }
        for p in (out.get("payloads") or [])
    ] + skipped
    return out


def execute_tool(
    db: Session,
    *,
    actor: str | None,
    perms: set[str],
    tool_name: str,
    arguments: dict[str, Any] | None,
    dry_run: bool = True,
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    arguments = arguments or {}
    tool = get_tool(tool_name)
    if tool is None:
        return {"ok": False, "error": "unknown_tool", "tool": tool_name}
    allowed, err = require_permission(perms, tool.required_permission)
    if not allowed:
        _audit(
            db,
            actor=actor,
            action="tool.denied",
            tool_name=tool_name,
            decision="deny",
            dry_run=dry_run,
            detail={"reason": err},
        )
        return {"ok": False, "error": err, "tool": tool_name}
    if not tool.available:
        return {
            "ok": False,
            "error": "NOT_CONFIGURED" if "not configured" in (tool.unavailable_reason or "").lower() else "UNSUPPORTED",
            "message": tool.unavailable_reason,
            "tool": tool_name,
        }
    if tool.confirmation_required and not dry_run and confirmation_token != "CONFIRM":
        return {
            "ok": False,
            "error": "confirmation_required",
            "tool": tool_name,
            "confirmation": {
                "action": tool_name,
                "side_effect_class": tool.side_effect_class.value,
                "arguments": arguments,
            },
        }

    # Canonical handlers
    if tool_name == "social.list_connections":
        rows = db.query(SocialConnection).order_by(SocialConnection.id.desc()).all()
        result = {
            "ok": True,
            "connections": [
                {
                    "id": c.id,
                    "provider": c.provider,
                    "display_name": c.display_name,
                    "status": c.status,
                    "health": c.health,
                }
                for c in rows
            ],
            "integrations": integration_matrix(),
        }
    elif tool_name == "content.create_draft":
        if dry_run:
            result = {
                "ok": True,
                "dry_run": True,
                "would_create": {
                    "title": arguments.get("title") or arguments.get("brief") or "Untitled",
                    "platforms": arguments.get("platforms") or ["telegram"],
                },
                "provider_called": False,
            }
        else:
            result = create_draft(
                db,
                actor=actor,
                title=str(arguments.get("title") or ""),
                brief=arguments.get("brief"),
                body=arguments.get("body"),
                platforms=list(arguments.get("platforms") or ["telegram"]),
                languages=list(arguments.get("languages") or ["EN"]),
            )
    elif tool_name == "content.generate_variants":
        platforms = list(arguments.get("platforms") or list(DEFAULT_VARIANT_PLATFORMS))
        languages = list(arguments.get("languages") or ["EN"])
        generated = generate_variants_local(str(arguments.get("brief") or ""), platforms, languages)
        if dry_run or arguments.get("persist") is not True:
            result = {**generated, "dry_run": True, "provider_called": False}
        else:
            created = create_draft(
                db,
                actor=actor,
                title=str(arguments.get("title") or arguments.get("brief") or "Generated draft")[:255],
                brief=str(arguments.get("brief") or ""),
                body=None,
                platforms=[],
                languages=[],
            )
            # replace empty variants with generated
            for v in generated["variants"]:
                db.add(
                    SocialContentVariant(
                        content_id=created["content_id"],
                        platform=v["platform"],
                        language=v["language"],
                        body=v["body"],
                        char_count=v["char_count"],
                        validation_json=json.dumps(v["validation"]),
                    )
                )
            db.flush()
            result = {"ok": True, "content_id": created["content_id"], "variants": generated["variants"], "ai_used": False}
    elif tool_name in {"content.translate", "content.rewrite"}:
        from src.social_agent import copilot as copilot_svc

        mode = "improve" if tool_name == "content.rewrite" else "grammar"
        if tool_name == "content.translate":
            # Local stub: language tag only until AI provider translation is certified.
            lang = str(arguments.get("language") or arguments.get("target_language") or "EN").upper()
            src = str(arguments.get("text") or arguments.get("body") or "")
            result = {
                "ok": True,
                "text": f"[{lang}] {src}".strip(),
                "language": lang,
                "ai_used": False,
                "provider_called": False,
                "message": "Local translate stub — AI provider translation not certified.",
            }
        else:
            result = copilot_svc.rewrite_content(
                str(arguments.get("text") or arguments.get("body") or ""),
                mode=str(arguments.get("mode") or mode),
            )
    elif tool_name == "content.transition_status":
        from src.social_agent import content_workflow as cw

        result = cw.transition_status(
            db,
            actor=actor,
            content_id=int(arguments["content_id"]),
            to_status=str(arguments.get("status") or arguments.get("to_status") or ""),
            note=arguments.get("note"),
        )
    elif tool_name == "content.studio_preview":
        from src.social_agent import content_workflow as cw

        result = cw.studio_previews(db, content_id=int(arguments["content_id"]))
    elif tool_name == "copilot.assist":
        from src.social_agent import copilot as copilot_svc

        result = copilot_svc.assist(
            db,
            intent=str(arguments.get("intent") or "help"),
            text=str(arguments.get("text") or arguments.get("brief") or ""),
            platform=str(arguments.get("platform") or "facebook"),
        )
    elif tool_name == "calendar.list":
        from src.social_agent import calendar_service as cal

        result = cal.list_entries(
            db,
            view=str(arguments.get("view") or "month"),
            anchor=arguments.get("anchor"),
        )
    elif tool_name == "calendar.queue":
        from src.social_agent import calendar_service as cal

        if dry_run:
            result = {
                "ok": True,
                "dry_run": True,
                "would_queue": {
                    "content_id": arguments.get("content_id"),
                    "platform": arguments.get("platform"),
                    "scheduled_for": arguments.get("scheduled_for"),
                },
                "live_publish_enabled": False,
            }
        else:
            result = cal.create_entry(
                db,
                actor=actor,
                content_id=int(arguments["content_id"]),
                platform=str(arguments.get("platform") or "facebook"),
                scheduled_for=str(arguments.get("scheduled_for") or ""),
                timezone_name=str(arguments.get("timezone") or "UTC"),
                notes=arguments.get("notes"),
            )
    elif tool_name == "analytics.get_summary":
        from src.social_agent import surfaces

        result = surfaces.analytics_architecture(db)
    elif tool_name == "comments.list":
        from src.social_agent import surfaces

        result = surfaces.comments_architecture()
    elif tool_name == "messages.list":
        from src.social_agent import surfaces

        result = surfaces.messages_architecture()
    elif tool_name == "automations.list":
        from src.social_agent import surfaces

        result = surfaces.automations_architecture(db)
    elif tool_name in {"publishing.preview", "publishing.dry_run"}:
        from src.social_agent.publishing.service import run_publishing_dry_run

        content_id = arguments.get("content_id")
        destinations = list(arguments.get("destinations") or ["facebook_page"])
        if content_id is not None:
            result = publish_preview(
                db,
                actor=actor,
                content_id=int(content_id),
                destinations=destinations,
                content=arguments.get("content") if isinstance(arguments.get("content"), dict) else None,
            )
        else:
            result = run_publishing_dry_run(
                db,
                actor=actor,
                destinations=destinations,
                content=arguments.get("content") if isinstance(arguments.get("content"), dict) else {
                    "text": arguments.get("text") or arguments.get("body") or arguments.get("brief") or "",
                    "hashtags": arguments.get("hashtags") or [],
                    "images": arguments.get("images") or [],
                    "videos": arguments.get("videos") or [],
                    "link": arguments.get("link"),
                    "cta": arguments.get("cta"),
                    "language": arguments.get("language") or "EN",
                    "brand_voice": arguments.get("brand_voice"),
                    "title": arguments.get("title"),
                },
            )
    elif tool_name == "publishing.facebook_canary":
        from src.social_agent.publishing import publish_service as canary_svc
        from src.social_agent.publishing.execution import CANARY_FACEBOOK_PAGE_ID

        action = str(arguments.get("action") or "prepare").strip().lower()
        if action == "prepare":
            result = canary_svc.prepare_facebook_canary_dry_run(db, actor=actor or "unknown")
        elif dry_run and action in {"authorize", "execute"}:
            result = {
                "ok": True,
                "dry_run": True,
                "would_run": action,
                "published": False,
                "provider_http_posts": 0,
                "message": "Dry-run only — canary authorize/execute require dry_run=false plus CONFIRM.",
            }
        elif action == "preflight":
            result = canary_svc.preflight_facebook_canary(
                db,
                actor=actor or "unknown",
                workspace_id="default",
                dry_run_id=int(arguments["dry_run_id"]),
                payload_hash=str(arguments["payload_hash"]),
                page_id=str(arguments.get("page_id") or CANARY_FACEBOOK_PAGE_ID),
            )
        elif action == "authorize":
            if confirmation_token != "CONFIRM" and arguments.get("explicit_approval") != "CONFIRM":
                result = {
                    "ok": False,
                    "error": "confirmation_required",
                    "confirmation": {"action": tool_name, "side_effect_class": "IMMEDIATE_EXTERNAL_MUTATION"},
                }
            else:
                result = canary_svc.approve_and_mint_canary(
                    db,
                    actor=actor or "unknown",
                    workspace_id="default",
                    dry_run_id=int(arguments["dry_run_id"]),
                    payload_hash=str(arguments["payload_hash"]),
                    page_id=str(arguments.get("page_id") or CANARY_FACEBOOK_PAGE_ID),
                    explicit_approval=str(arguments.get("explicit_approval") or confirmation_token or ""),
                    idempotency_key=arguments.get("idempotency_key"),
                )
        elif action == "execute":
            result = canary_svc.execute_facebook_canary(
                db,
                actor=actor or "unknown",
                workspace_id="default",
                authorization_id=int(arguments["authorization_id"]),
                authorization_secret=str(arguments["authorization_secret"]),
                dry_run_id=int(arguments["dry_run_id"]),
                payload_hash=str(arguments["payload_hash"]),
                page_id=str(arguments.get("page_id") or CANARY_FACEBOOK_PAGE_ID),
                idempotency_key=str(arguments["idempotency_key"]),
            )
        else:
            result = {"ok": False, "error": "unknown_canary_action", "action": action}
    elif tool_name == "media.list_assets":
        from src.social_agent import media_library as media

        result = media.list_assets(
            db,
            folder_id=int(arguments["folder_id"]) if arguments.get("folder_id") is not None else None,
            limit=int(arguments.get("limit") or 100),
        )
    elif tool_name == "media.create_folder":
        from src.social_agent import media_library as media

        if dry_run:
            result = {"ok": True, "dry_run": True, "would_create_folder": arguments.get("name")}
        else:
            result = media.create_folder(
                db,
                actor=actor,
                name=str(arguments.get("name") or ""),
                parent_id=int(arguments["parent_id"]) if arguments.get("parent_id") is not None else None,
            )
    elif tool_name == "brand.search_knowledge":
        from src.social_agent import brand_store as brand

        result = brand.search_knowledge(db, query=str(arguments.get("query") or ""))
    elif tool_name == "brand.list_knowledge":
        from src.social_agent import brand_store as brand

        result = brand.list_knowledge(db)
    elif tool_name == "brand.upsert":
        from src.social_agent import brand_store as brand

        if dry_run:
            result = {"ok": True, "dry_run": True, "would_upsert": arguments}
        else:
            result = brand.upsert_knowledge(
                db,
                actor=actor,
                category=str(arguments.get("category") or ""),
                key=str(arguments.get("key") or ""),
                title=str(arguments.get("title") or ""),
                value=str(arguments.get("value") or ""),
            )
    else:
        result = {"ok": False, "error": "handler_missing", "tool": tool_name}

    _audit(
        db,
        actor=actor,
        action="tool.execute",
        tool_name=tool_name,
        decision="allow" if result.get("ok") else "deny",
        dry_run=dry_run,
        detail={"arguments_keys": sorted(arguments.keys()), "error": result.get("error")},
    )
    return result


def chat_turn(
    db: Session,
    *,
    actor: str | None,
    perms: set[str],
    conversation_id: int | None,
    message: str,
) -> dict[str, Any]:
    """Assistant turn: persist user message, run safe local tools, return assistant reply."""
    if conversation_id is None:
        created = create_conversation(db, actor=actor, title=(message or "Chat")[:60])
        conversation_id = int(created["conversation_id"])
    append_message(db, conversation_id=int(conversation_id), role="user", content=message)
    text = (message or "").strip()
    lower = text.lower()
    tool_calls: list[dict[str, Any]] = []
    reply_parts: list[str] = []

    wants_draft = any(
        k in lower
        for k in (
            "generate",
            "create a",
            "create post",
            "draft",
            "write a",
            "make a",
            "вариан",
            "пост",
        )
    )
    wants_publish = any(
        k in lower
        for k in (
            "publish",
            "dry-run",
            "dry run",
            "preview publish",
            "post to facebook",
            "post to instagram",
            "опублик",
        )
    )
    if wants_publish:
        destinations: list[str] = []
        if "carousel" in lower:
            destinations.append("instagram_carousel")
        if "story" in lower or "stories" in lower:
            destinations.append("instagram_story")
        if "instagram" in lower or " ig" in lower:
            if "instagram_feed" not in destinations and "instagram_carousel" not in destinations and "instagram_story" not in destinations:
                destinations.append("instagram_feed")
        if "facebook" in lower or " fb" in lower or "page" in lower:
            destinations.append("facebook_page")
        if not destinations:
            destinations = ["facebook_page"]

        draft_result = execute_tool(
            db,
            actor=actor,
            perms=perms,
            tool_name="content.generate_variants",
            arguments={
                "brief": text,
                "title": (text[:80] or "Publish dry-run"),
                "platforms": ["facebook", "instagram"],
                "languages": ["EN"],
                "persist": True,
            },
            dry_run=False,
        )
        tool_calls.append({"tool": "content.generate_variants", "result": draft_result})
        if not draft_result.get("ok"):
            reply_parts.append(f"Could not create draft: {draft_result.get('error') or draft_result.get('message')}")
        else:
            content_id = int(draft_result["content_id"])
            # Default media for IG destinations so validation can produce a payload preview.
            content_payload: dict[str, Any] = {
                "text": text,
                "hashtags": ["Exswaping", "USDT"],
                "language": "EN",
                "brand_voice": "clear, trustworthy",
            }
            if any(d.startswith("instagram") for d in destinations):
                if "carousel" in lower:
                    content_payload["images"] = [
                        {"url": "https://cdn.example.com/rates-1.jpg"},
                        {"url": "https://cdn.example.com/rates-2.jpg"},
                    ]
                else:
                    content_payload["images"] = [{"url": "https://cdn.example.com/rates.jpg"}]
            dry = execute_tool(
                db,
                actor=actor,
                perms=perms,
                tool_name="publishing.dry_run",
                arguments={"content_id": content_id, "destinations": destinations, "content": content_payload},
                dry_run=True,
            )
            tool_calls.append({"tool": "publishing.dry_run", "result": dry})
            reply_parts.append(f"Draft created: #{content_id}")
            validation = dry.get("validation") or {}
            reply_parts.append(
                "Validation: "
                + ("OK" if validation.get("ok") else "FAILED")
                + (f" · status={dry.get('status')}" if dry.get("status") else "")
            )
            payloads = dry.get("payloads") or []
            if payloads:
                summary = ", ".join(
                    f"{p.get('destination')} → {p.get('endpoint') or ((p.get('steps') or [{}])[0].get('endpoint'))}"
                    for p in payloads
                )
                reply_parts.append(f"Payload preview: {summary}")
            else:
                reply_parts.append("Payload preview: none (fix failed).")
            if dry.get("ready_for_publishing"):
                reply_parts.append("Ready for publishing (dry-run only). Nothing was published.")
            else:
                reply_parts.append("Not ready for publishing. Nothing was published.")
            reply_parts.append(
                f"Dry-run id #{dry.get('dry_run_id')} · hash={dry.get('payload_hash')} · "
                f"provider_called={dry.get('provider_called')} · META_PROVIDER_MUTATIONS={dry.get('meta_provider_mutations', 0)}"
            )
    elif wants_draft:
        platforms = list(DEFAULT_VARIANT_PLATFORMS)
        languages = ["EN"]
        if "armenian" in lower or "հայերեն" in lower or " hy" in lower:
            languages.append("HY")
        if "russian" in lower or "рус" in lower or " ru" in lower:
            languages.append("RU")
        result = execute_tool(
            db,
            actor=actor,
            perms=perms,
            tool_name="content.generate_variants",
            arguments={"brief": text, "platforms": platforms, "languages": languages, "persist": True},
            dry_run=False,
        )
        tool_calls.append({"tool": "content.generate_variants", "result": result})
        if result.get("ok"):
            reply_parts.append(
                f"Created draft #{result.get('content_id')} with {len(result.get('variants') or [])} variants. "
                "Nothing was published. Open Content Studio to edit, approve, or dry-run publish."
            )
        else:
            reply_parts.append(f"Could not create draft: {result.get('error') or result.get('message')}")
    elif any(k in lower for k in ("hashtag", "rewrite", "compliance", "grammar", "image prompt", "calendar plan", "engagement")):
        from src.social_agent import copilot as copilot_svc

        intent = "hashtags"
        if "rewrite" in lower or "improve" in lower:
            intent = "rewrite"
        elif "compliance" in lower or "banned" in lower:
            intent = "compliance"
        elif "grammar" in lower:
            intent = "grammar"
        elif "image" in lower:
            intent = "image_prompt"
        elif "calendar" in lower:
            intent = "calendar"
        elif "engagement" in lower or "predict" in lower:
            intent = "engagement"
        assist = copilot_svc.assist(db, intent=intent, text=text)
        tool_calls.append({"tool": "copilot.assist", "result": assist})
        reply_parts.append(json.dumps({k: assist.get(k) for k in assist if k != "brand_categories"}, indent=2)[:2500])
        reply_parts.append("Auto-publish remains disabled.")
    elif any(k in lower for k in ("connection", "account health", "meta status", "telegram status", "connected")):
        result = execute_tool(
            db,
            actor=actor,
            perms=perms,
            tool_name="social.list_connections",
            arguments={},
            dry_run=True,
        )
        tool_calls.append({"tool": "social.list_connections", "result": result})
        integ = (result.get("integrations") or {})
        reply_parts.append(
            "Connection status:\n"
            f"- Meta: {integ.get('meta', {}).get('status')}\n"
            f"- Telegram: {integ.get('telegram', {}).get('status')} ({integ.get('telegram', {}).get('message')})\n"
            f"- AI: {integ.get('ai', {}).get('status')}"
        )
    else:
        from src.social_agent.copilot import copilot_capabilities
        from src.social_agent.brand_store import brand_context_for_ai

        brand = brand_context_for_ai(db)
        reply_parts.append(
            "I'm the Social Agent copilot. I can generate campaigns/variants, rewrite copy, "
            "suggest hashtags, draft image prompts, check compliance, outline calendars, "
            "and prepare dry-run publish previews. I never publish automatically."
        )
        reply_parts.append("Capabilities: " + ", ".join(copilot_capabilities()))
        reply_parts.append(
            "Brand categories loaded: " + ", ".join(sorted((brand.get("brand") or {}).keys()) or ["(seed pending)"])
        )
        reply_parts.append(
            'Try: “Create a multi-platform draft about USDT to AMD”, “Rewrite this for LinkedIn”, '
            '“Suggest hashtags”, or “Publish dry-run to Facebook”.'
        )

    reply = "\n\n".join(reply_parts)
    append_message(
        db,
        conversation_id=int(conversation_id),
        role="assistant",
        content=reply,
        tool_payload={"tool_calls": tool_calls} if tool_calls else None,
    )
    return {
        "ok": True,
        "conversation_id": conversation_id,
        "reply": reply,
        "tool_calls": tool_calls,
        "streaming": False,
        "ai_provider_used": False,
    }
