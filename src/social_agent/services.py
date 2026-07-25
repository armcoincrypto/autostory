"""Canonical Social Agent application services (UI and AI share these)."""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy.orm import Session

from src.social_agent.integrations import integration_matrix, meta_status, telegram_adapter_status
from src.social_agent.models import (
    SocialAgentAuditEvent,
    SocialAgentConversation,
    SocialAgentMessage,
    SocialConnection,
    SocialContentItem,
    SocialContentVariant,
)
from src.social_agent.permissions import require_permission
from src.social_agent.tools import get_tool, list_tools

logger = structlog.get_logger(__name__)

PLATFORM_LIMITS = {
    "facebook": 63206,
    "instagram": 2200,
    "telegram": 4096,
    "x": 280,
    "linkedin": 3000,
}


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
        .filter(SocialContentItem.status.in_(["DRAFT", "READY_FOR_REVIEW"]))
        .order_by(SocialContentItem.id.desc())
        .limit(10)
        .all()
    )
    recent_msgs = db.query(SocialAgentMessage).order_by(SocialAgentMessage.id.desc()).limit(5).all()
    integ = integration_matrix()
    meta_sum = _meta_connection_summary(connections)
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
            {"id": d.id, "title": d.title, "status": d.status} for d in drafts
        ],
        "scheduled_posts": [],
        "publishing_failures": [],
        "recent_successful_posts": [],
        "upcoming_calendar": [],
        "recent_ai_activity": [
            {"id": m.id, "role": m.role, "preview": (m.content or "")[:120]} for m in recent_msgs
        ],
        "integrations": integ,
        "alerts": _build_alerts(integ, connections),
        "onboarding": _onboarding(integ, connections),
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
    if integ["exswaping"]["status"] == "NOT_CONFIGURED":
        alerts.append("Exswaping public-content API not configured.")
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
    steps.append("Add Exswaping content access once the official public-content API exists.")
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
    out = []
    for platform in platforms:
        for lang in languages:
            prefix = {
                "EN": "",
                "RU": "[RU] ",
                "HY": "[HY] ",
            }.get(lang.upper(), f"[{lang}] ")
            body = f"{prefix}{brief}".strip()
            if platform == "x" and len(body) > 280:
                body = body[:277] + "..."
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
            "status": item.status,
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
) -> dict[str, Any]:
    draft = get_draft(db, content_id)
    if not draft.get("ok"):
        return draft
    dest_results = []
    for dest in destinations:
        provider = dest.lower()
        if provider in {"facebook", "instagram"}:
            meta = meta_status()
            dest_results.append(
                {
                    "destination": provider,
                    "ready": meta["configured"] is True and False,  # not connected yet
                    "reason": "meta_not_connected" if meta["configured"] else "meta_credentials_missing",
                }
            )
        elif provider == "telegram":
            tg = telegram_adapter_status()
            dest_results.append(
                {
                    "destination": "telegram",
                    "ready": False,
                    "reason": "telegram_live_publish_fail_closed",
                    "adapter": tg,
                }
            )
        else:
            dest_results.append(
                {"destination": provider, "ready": False, "reason": "provider_not_started"}
            )
    idem = f"preview:{content_id}:{uuid.uuid4().hex[:12]}"
    _audit(
        db,
        actor=actor,
        action="publishing.preview",
        tool_name="publishing.preview",
        decision="dry_run",
        dry_run=True,
        detail={"content_id": content_id, "destinations": dest_results, "idempotency_key": idem},
    )
    return {
        "ok": True,
        "dry_run": True,
        "idempotency_key": idem,
        "content": draft["item"],
        "variants": draft["variants"],
        "destinations": dest_results,
        "provider_called": False,
        "external_ids": {},
    }


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
        platforms = list(arguments.get("platforms") or ["facebook", "instagram", "telegram", "x", "linkedin"])
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
    elif tool_name == "publishing.preview":
        result = publish_preview(
            db,
            actor=actor,
            content_id=int(arguments["content_id"]),
            destinations=list(arguments.get("destinations") or ["telegram"]),
        )
    elif tool_name == "media.list_assets":
        result = {"ok": True, "assets": [], "message": "Media library empty — upload not yet enabled in this release."}
    elif tool_name == "brand.search_knowledge":
        q = str(arguments.get("query") or "").lower()
        rules = [
            {"id": "tone", "title": "Exswaping tone", "text": "Clear, trustworthy, no guaranteed returns."},
            {"id": "aml", "title": "AML guidance", "text": "Do not promise bypass of KYC/AML."},
            {"id": "cta", "title": "Approved CTA", "text": "Use official Exswaping destination links only."},
        ]
        hits = [r for r in rules if not q or q in r["title"].lower() or q in r["text"].lower()]
        result = {"ok": True, "hits": hits, "source": "bundled_seed_rules"}
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
    if wants_draft:
        platforms = ["facebook", "instagram", "telegram", "x", "linkedin"]
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
            f"- Exswaping: {integ.get('exswaping', {}).get('status')}"
        )
    else:
        reply_parts.append(
            "I'm the Social Agent assistant. I can create drafts, generate platform variants, "
            "check connection status, and prepare dry-run publish previews. "
            "Live publishing requires confirmation and an authorized canary. "
            "Try: “Create a Telegram post about USDT to AMD”."
        )
        reply_parts.append(f"Available tools: {', '.join(t['name'] for t in list_tools()[:8])}…")

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
