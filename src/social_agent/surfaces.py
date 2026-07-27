"""Architecture surfaces for Analytics, Comments, Messages, Automations.

Honest empty / not-configured responses. No fabricated metrics, no polling,
no provider writes, automations always disabled.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from src.social_agent.integrations import integration_matrix
from src.social_agent.models import SocialAutomationDefinition

ANALYTICS_CARDS = (
    "followers",
    "reach",
    "impressions",
    "clicks",
    "ctr",
    "engagement",
    "top_posts",
    "growth",
)


def analytics_architecture(db: Session | None = None) -> dict[str, Any]:
    integ = integration_matrix()
    providers = []
    for key in ("meta", "telegram", "x", "linkedin", "discord", "tiktok", "youtube"):
        info = integ.get(key) or {}
        status = info.get("status") or "NOT_CONFIGURED"
        available = status in {"BACKEND_READY", "CONNECTED"} and key == "meta"
        # Meta connection may exist, but analytics Graph scopes are not certified.
        providers.append(
            {
                "provider": key,
                "status": status if key != "meta" else "ANALYTICS_NOT_CERTIFIED",
                "available": False,
                "message": "No analytics available.",
            }
        )
    return {
        "ok": True,
        "architecture": {
            "cards": list(ANALYTICS_CARDS),
            "aggregation": "provider_adapter",
            "caching": "planned",
            "fabricated_metrics": False,
        },
        "cards": [
            {"id": c, "label": c.replace("_", " ").title(), "value": None, "available": False}
            for c in ANALYTICS_CARDS
        ],
        "providers": providers,
        "message": "No analytics available.",
        "provider_called": False,
    }


def comments_architecture() -> dict[str, Any]:
    return {
        "ok": True,
        "architecture": {
            "model": "provider_comment_thread",
            "polling": False,
            "inbound": "webhook_or_manual_sync_later",
            "replies": "disabled_fail_closed",
        },
        "threads": [],
        "message": "No comments available. Provider comment sync is not configured.",
        "provider_called": False,
    }


def messages_architecture() -> dict[str, Any]:
    return {
        "ok": True,
        "architecture": {
            "model": "provider_conversation",
            "inbox": "unified",
            "provider_writes": False,
            "delivery": "disabled_fail_closed",
        },
        "conversations": [],
        "message": "No messages available. Provider messaging is not configured.",
        "provider_called": False,
    }


def automations_architecture(db: Session, *, workspace_id: str = "default") -> dict[str, Any]:
    rows = (
        db.query(SocialAutomationDefinition)
        .filter(SocialAutomationDefinition.workspace_id == workspace_id)
        .order_by(SocialAutomationDefinition.id.desc())
        .limit(50)
        .all()
    )
    return {
        "ok": True,
        "architecture": {
            "builder": "visual_workflow",
            "nodes": ["trigger", "condition", "action"],
            "execution": "disabled",
            "live_automation": False,
        },
        "definitions": [
            {
                "id": r.id,
                "name": r.name,
                "enabled": False,  # hard override
                "definition": json.loads(r.definition_json or "{}"),
            }
            for r in rows
        ],
        "live_automation_enabled": False,
        "message": "Automations are definition-only. Execution remains disabled.",
    }


def save_automation_draft(
    db: Session,
    *,
    actor: str | None,
    name: str,
    definition: dict[str, Any] | None,
    workspace_id: str = "default",
) -> dict[str, Any]:
    label = (name or "").strip()[:255] or "Untitled workflow"
    # Force disabled regardless of caller input.
    row = SocialAutomationDefinition(
        workspace_id=workspace_id,
        name=label,
        enabled=False,
        definition_json=json.dumps(definition or {"triggers": [], "conditions": [], "actions": []}),
        created_by=actor,
    )
    db.add(row)
    db.flush()
    return {
        "ok": True,
        "id": row.id,
        "name": row.name,
        "enabled": False,
        "message": "Saved as disabled draft. Live automation is not available.",
    }


def accounts_provider_matrix(db: Session) -> dict[str, Any]:
    """Honest multi-provider Social Accounts panel — no fake connected rows."""
    from src.social_agent.models import SocialConnection

    integ = integration_matrix()
    connections = db.query(SocialConnection).order_by(SocialConnection.id.desc()).all()
    by_provider: dict[str, list[dict[str, Any]]] = {}
    for c in connections:
        by_provider.setdefault(c.provider, []).append(
            {
                "id": c.id,
                "display_name": c.display_name,
                "status": c.status,
                "health": c.health,
                "token_expires_at": c.token_expires_at.isoformat() if c.token_expires_at else None,
                "selected_page_id": c.selected_page_id,
                "selected_instagram_id": c.selected_instagram_id,
                "permissions": json.loads(c.permissions_json or "{}") if c.permissions_json else {},
                "last_checked_at": c.last_checked_at.isoformat() if c.last_checked_at else None,
            }
        )

    providers = []
    for key, label in (
        ("meta", "Meta"),
        ("telegram", "Telegram"),
        ("x", "X"),
        ("linkedin", "LinkedIn"),
        ("discord", "Discord"),
        ("tiktok", "TikTok"),
        ("youtube", "YouTube"),
    ):
        info = integ.get(key) or {"status": "NOT_CONFIGURED"}
        status = info.get("status") or "NOT_CONFIGURED"
        configured = bool(info.get("configured")) if "configured" in info else status not in {
            "NOT_STARTED",
            "NOT_CONFIGURED",
            "CREDENTIALS_MISSING",
        }
        rows = by_provider.get(key) or []
        if key == "meta" and rows:
            panel_status = "CONNECTED" if any(r.get("status") == "connected" for r in rows) else status
        elif key == "telegram" and status == "BACKEND_READY":
            panel_status = "BACKEND_READY"
        elif not configured and not rows:
            panel_status = "NOT CONFIGURED"
        else:
            panel_status = status.replace("_", " ")
        providers.append(
            {
                "provider": key,
                "label": label,
                "status": panel_status,
                "configured": configured,
                "connections": rows,
                "capabilities": info.get("capabilities") or [],
                "message": info.get("message") or ("NOT CONFIGURED" if panel_status == "NOT CONFIGURED" else None),
                "token_expiry": rows[0].get("token_expires_at") if rows else None,
                "permissions": rows[0].get("permissions") if rows else {},
                "selected_destination": {
                    "page_id": rows[0].get("selected_page_id"),
                    "instagram_id": rows[0].get("selected_instagram_id"),
                }
                if rows
                else None,
                "health": rows[0].get("health") if rows else None,
            }
        )
    return {"ok": True, "providers": providers, "fabricated": False}
