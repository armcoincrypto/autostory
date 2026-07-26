"""Social Agent pages + JSON API (canonical services shared with AI tools)."""
from __future__ import annotations

import os
from typing import Any

import structlog
from flask import Blueprint, jsonify, redirect, render_template, request, url_for
from flask_login import current_user

from src.core.database import get_db_context
from src.dashboard.auth_access import dashboard_api_authorized
from src.social_agent import services
from src.social_agent.integrations import integration_matrix, meta_status
from src.social_agent.permissions import actor_permissions
from src.social_agent.registry import get_agent, list_platform_agents
from src.social_agent.tools import list_tools

# Ensure models are registered on Base.metadata before create_all.
import src.social_agent.models  # noqa: F401

logger = structlog.get_logger(__name__)

social_agent_bp = Blueprint("social_agent", __name__)
social_agent_api = Blueprint("social_agent_api", __name__, url_prefix="/api/v1/social-agent")

NAV = [
    {"id": "overview", "label": "Overview", "path": "/social-agent"},
    {"id": "assistant", "label": "AI Assistant", "path": "/social-agent/assistant"},
    {"id": "content", "label": "Content Studio", "path": "/social-agent/content"},
    {"id": "publishing", "label": "Publishing", "path": "/social-agent/publishing", "badge": "preview"},
    {"id": "calendar", "label": "Calendar", "path": "/social-agent/calendar", "badge": "coming_later"},
    {"id": "media", "label": "Media Library", "path": "/social-agent/media", "badge": "coming_later"},
    {"id": "accounts", "label": "Social Accounts", "path": "/social-agent/accounts"},
    {"id": "analytics", "label": "Analytics", "path": "/social-agent/analytics", "badge": "coming_later"},
    {"id": "comments", "label": "Comments", "path": "/social-agent/comments", "badge": "coming_later"},
    {"id": "messages", "label": "Messages", "path": "/social-agent/messages", "badge": "coming_later"},
    {"id": "brand", "label": "Brand Knowledge", "path": "/social-agent/brand"},
    {"id": "automations", "label": "Automations", "path": "/social-agent/automations", "badge": "coming_later"},
    {"id": "settings", "label": "Settings", "path": "/social-agent/settings"},
]


def _require_auth_api():
    if not dashboard_api_authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    return None


def _actor() -> str:
    if current_user.is_authenticated:
        return str(getattr(current_user, "username", None) or getattr(current_user, "id", "dashboard_user"))
    return "admin_token"


def _perms() -> set[str]:
    return actor_permissions(is_dashboard_admin=True)


def _page(template: str, section: str, **ctx: Any):
    if not dashboard_api_authorized():
        return redirect(url_for("auth.login", next=request.full_path or request.path))
    return render_template(
        template,
        section=section,
        nav=NAV,
        agent=get_agent("social_agent"),
        **ctx,
    )


@social_agent_bp.before_request
def _page_auth():
    if request.endpoint and request.endpoint.startswith("social_agent."):
        if not dashboard_api_authorized():
            return redirect(url_for("auth.login", next=request.full_path or request.path))
    return None


@social_agent_api.before_request
def _api_auth():
    return _require_auth_api()


# --- Agents launcher ---
@social_agent_bp.route("/agents", methods=["GET"])
def agents_page():
    return render_template(
        "social_agent/agents.html",
        agents=list_platform_agents(),
        active_agent_id="social_agent",
    )


# --- Social Agent workspace pages ---
@social_agent_bp.route("/social-agent", methods=["GET"])
def overview_page():
    return _page("social_agent/overview.html", "overview")


@social_agent_bp.route("/social-agent/assistant", methods=["GET"])
def assistant_page():
    return _page("social_agent/assistant.html", "assistant")


@social_agent_bp.route("/social-agent/content", methods=["GET"])
def content_page():
    return _page("social_agent/content.html", "content")


@social_agent_bp.route("/social-agent/publishing", methods=["GET"])
def publishing_page():
    return _page("social_agent/publishing.html", "publishing")


@social_agent_bp.route("/social-agent/calendar", methods=["GET"])
def calendar_page():
    return _page("social_agent/placeholder.html", "calendar", title="Calendar", reason="Coming later — scheduling certification required.")


@social_agent_bp.route("/social-agent/media", methods=["GET"])
def media_page():
    return _page("social_agent/placeholder.html", "media", title="Media Library", reason="Coming later — upload pipeline not enabled in first release.")


@social_agent_bp.route("/social-agent/accounts", methods=["GET"])
def accounts_page():
    return _page("social_agent/accounts.html", "accounts")


@social_agent_bp.route("/social-agent/analytics", methods=["GET"])
def analytics_page():
    return _page("social_agent/placeholder.html", "analytics", title="Analytics", reason="Provider analytics not configured.")


@social_agent_bp.route("/social-agent/comments", methods=["GET"])
def comments_page():
    return _page("social_agent/placeholder.html", "comments", title="Comments", reason="Coming later — provider support required.")


@social_agent_bp.route("/social-agent/messages", methods=["GET"])
def messages_page():
    return _page("social_agent/placeholder.html", "messages", title="Messages", reason="Coming later — provider support required.")


@social_agent_bp.route("/social-agent/brand", methods=["GET"])
def brand_page():
    return _page("social_agent/brand.html", "brand")


@social_agent_bp.route("/social-agent/automations", methods=["GET"])
def automations_page():
    return _page(
        "social_agent/placeholder.html",
        "automations",
        title="Automations",
        reason="Suggestion-only in later phases — autonomous publishing disabled.",
    )


@social_agent_bp.route("/social-agent/settings", methods=["GET"])
def settings_page():
    return _page("social_agent/settings.html", "settings")


# --- API ---
@social_agent_api.route("/health", methods=["GET"])
def health():
    return jsonify(
        {
            "ok": True,
            "status": "ok",
            "release_sha": (os.environ.get("AUTOSTORY_RELEASE_SHA") or "")[:40] or None,
            "integrations": {
                k: {"status": v.get("status"), "configured": v.get("configured")}
                for k, v in integration_matrix().items()
                if isinstance(v, dict)
            },
        }
    )


@social_agent_api.route("/agents", methods=["GET"])
def api_agents():
    return jsonify({"ok": True, "agents": list_platform_agents()})


@social_agent_api.route("/tools", methods=["GET"])
def api_tools():
    return jsonify({"ok": True, "tools": list_tools()})


@social_agent_api.route("/overview", methods=["GET"])
def api_overview():
    with get_db_context() as db:
        return jsonify(services.overview(db))


@social_agent_api.route("/conversations", methods=["GET"])
def api_list_conversations():
    with get_db_context() as db:
        return jsonify(services.list_conversations(db))


@social_agent_api.route("/conversations", methods=["POST"])
def api_create_conversation():
    data = request.get_json(silent=True) or {}
    with get_db_context() as db:
        out = services.create_conversation(db, actor=_actor(), title=data.get("title"))
        db.commit()
        return jsonify(out)


@social_agent_api.route("/conversations/<int:conversation_id>/messages", methods=["GET"])
def api_list_messages(conversation_id: int):
    with get_db_context() as db:
        return jsonify(services.list_messages(db, conversation_id))


@social_agent_api.route("/chat", methods=["POST"])
def api_chat():
    data = request.get_json(silent=True) or {}
    message = str(data.get("message") or "").strip()
    if not message:
        return jsonify({"ok": False, "error": "message_required"}), 400
    with get_db_context() as db:
        out = services.chat_turn(
            db,
            actor=_actor(),
            perms=_perms(),
            conversation_id=data.get("conversation_id"),
            message=message,
        )
        db.commit()
        return jsonify(out)


@social_agent_api.route("/tools/execute", methods=["POST"])
def api_tool_execute():
    data = request.get_json(silent=True) or {}
    tool_name = str(data.get("tool") or data.get("name") or "").strip()
    if not tool_name:
        return jsonify({"ok": False, "error": "tool_required"}), 400
    with get_db_context() as db:
        out = services.execute_tool(
            db,
            actor=_actor(),
            perms=_perms(),
            tool_name=tool_name,
            arguments=data.get("arguments") or {},
            dry_run=bool(data.get("dry_run", True)),
            confirmation_token=data.get("confirmation_token"),
        )
        db.commit()
        status = 200 if out.get("ok") else 400
        return jsonify(out), status


@social_agent_api.route("/content", methods=["GET"])
def api_list_content():
    with get_db_context() as db:
        return jsonify(services.list_drafts(db))


@social_agent_api.route("/content", methods=["POST"])
def api_create_content():
    data = request.get_json(silent=True) or {}
    with get_db_context() as db:
        out = services.create_draft(
            db,
            actor=_actor(),
            title=str(data.get("title") or ""),
            brief=data.get("brief"),
            body=data.get("body"),
            platforms=list(data.get("platforms") or ["telegram"]),
            languages=list(data.get("languages") or ["EN"]),
        )
        db.commit()
        return jsonify(out)


@social_agent_api.route("/content/<int:content_id>", methods=["GET"])
def api_get_content(content_id: int):
    with get_db_context() as db:
        out = services.get_draft(db, content_id)
        return jsonify(out), (200 if out.get("ok") else 404)


@social_agent_api.route("/publishing/preview", methods=["POST"])
def api_publish_preview():
    data = request.get_json(silent=True) or {}
    try:
        content_id = int(data["content_id"])
    except Exception:
        return jsonify({"ok": False, "error": "content_id_required"}), 400
    with get_db_context() as db:
        out = services.publish_preview(
            db,
            actor=_actor(),
            content_id=content_id,
            destinations=list(data.get("destinations") or ["telegram"]),
        )
        db.commit()
        return jsonify(out)


@social_agent_api.route("/integrations", methods=["GET"])
def api_integrations():
    return jsonify({"ok": True, "integrations": integration_matrix()})


def _require_manage_accounts():
    perms = _perms()
    from src.social_agent.permissions import require_permission

    ok, err = require_permission(perms, "social_accounts.manage")
    if not ok:
        return jsonify({"ok": False, "error": err}), 403
    return None


@social_agent_api.route("/connections/meta", methods=["GET"])
def api_meta_connection():
    with get_db_context() as db:
        from src.social_agent import meta_connection as meta_svc

        return jsonify(meta_svc.get_meta_connection(db))


@social_agent_api.route("/connections/meta/start", methods=["POST", "GET"])
def api_meta_start():
    denied = _require_manage_accounts()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    with get_db_context() as db:
        from src.social_agent import meta_connection as meta_svc

        out = meta_svc.start_meta_oauth(
            db,
            actor=_actor(),
            purpose=str(data.get("purpose") or "connect"),
            connection_id=data.get("connection_id"),
        )
        db.commit()
        status = 200 if out.get("ok") else 400
        if out.get("error") == "CREDENTIALS_MISSING":
            status = 503
        return jsonify(out), status


@social_agent_bp.route("/social-agent/accounts/meta/callback", methods=["GET"])
def meta_oauth_callback_page():
    """Browser OAuth callback — state validated server-side; never logs code."""
    if not dashboard_api_authorized():
        return redirect(url_for("auth.login", next="/social-agent/accounts"))
    with get_db_context() as db:
        from src.social_agent import meta_connection as meta_svc

        out = meta_svc.handle_meta_callback(
            db,
            actor=_actor(),
            state=request.args.get("state"),
            code=request.args.get("code"),
            error=request.args.get("error"),
            error_description=request.args.get("error_description"),
        )
        db.commit()
    # Safe internal redirect only — never open redirect.
    if out.get("ok"):
        return redirect("/social-agent/accounts?meta=connected")
    reason = out.get("error") or "callback_failed"
    return redirect(f"/social-agent/accounts?meta_error={reason}")


@social_agent_api.route("/meta/oauth/start", methods=["GET", "POST"])
def meta_oauth_start():
    """Backward-compatible alias — starts OAuth when configured."""
    return api_meta_start()


@social_agent_api.route("/connections/meta/select", methods=["POST"])
def api_meta_select():
    denied = _require_manage_accounts()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    try:
        connection_id = int(data["connection_id"])
        page_id = str(data["page_id"])
    except Exception:
        return jsonify({"ok": False, "error": "connection_id_and_page_id_required"}), 400
    with get_db_context() as db:
        from src.social_agent import meta_connection as meta_svc

        out = meta_svc.select_meta_destination(
            db,
            actor=_actor(),
            connection_id=connection_id,
            page_id=page_id,
            instagram_account_id=data.get("instagram_account_id"),
        )
        db.commit()
        return jsonify(out), (200 if out.get("ok") else 400)


@social_agent_api.route("/connections/meta/pages", methods=["POST", "GET"])
def api_meta_rediscover_pages():
    """Re-run read-only Page discovery and return every discovered Page for operator selection."""
    denied = _require_manage_accounts()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    with get_db_context() as db:
        from src.social_agent import meta_connection as meta_svc

        out = meta_svc.rediscover_meta_pages(
            db,
            actor=_actor(),
            connection_id=data.get("connection_id") or request.args.get("connection_id", type=int),
        )
        db.commit()
        return jsonify(out), (200 if out.get("ok") else 400)


@social_agent_api.route("/connections/meta/health", methods=["POST", "GET"])
def api_meta_health():
    data = request.get_json(silent=True) or {}
    with get_db_context() as db:
        from src.social_agent import meta_connection as meta_svc

        out = meta_svc.meta_health_check(
            db,
            actor=_actor(),
            connection_id=data.get("connection_id") or request.args.get("connection_id", type=int),
        )
        db.commit()
        return jsonify(out)


@social_agent_api.route("/connections/meta/reconnect", methods=["POST"])
def api_meta_reconnect():
    denied = _require_manage_accounts()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    with get_db_context() as db:
        from src.social_agent import meta_connection as meta_svc

        out = meta_svc.start_meta_oauth(
            db,
            actor=_actor(),
            purpose="reconnect",
            connection_id=data.get("connection_id"),
        )
        db.commit()
        return jsonify(out), (200 if out.get("ok") else 400)


@social_agent_api.route("/connections/meta/disconnect", methods=["POST"])
def api_meta_disconnect():
    denied = _require_manage_accounts()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    try:
        connection_id = int(data["connection_id"])
    except Exception:
        return jsonify({"ok": False, "error": "connection_id_required"}), 400
    with get_db_context() as db:
        from src.social_agent import meta_connection as meta_svc

        out = meta_svc.disconnect_meta(
            db,
            actor=_actor(),
            connection_id=connection_id,
            confirm=bool(data.get("confirm")),
        )
        db.commit()
        return jsonify(out), (200 if out.get("ok") else 400)


@social_agent_api.route("/connections/meta/publish", methods=["POST"])
def api_meta_publish_blocked():
    from src.social_agent.meta_connection import assert_meta_publishing_disabled

    return jsonify(assert_meta_publishing_disabled()), 403


@social_agent_api.route("/debug", methods=["GET"])
def debug_safe():
    """Operator debug — auth already required; additionally fail-closed unless enabled."""
    enabled = (os.environ.get("SOCIAL_AGENT_DEBUG_ENABLED") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if not enabled:
        return jsonify({"ok": False, "error": "debug_disabled"}), 404
    from src.social_agent.credential_crypto import SocialCredentialCrypto
    from src.social_agent.providers.meta import MetaProviderAdapter

    cfg = MetaProviderAdapter.config()
    return jsonify(
        {
            "ok": True,
            "APP_ENV": os.environ.get("FLASK_ENV") or os.environ.get("APP_ENV") or "production",
            "DATABASE_CONFIGURED": True,
            "QUEUE_CONFIGURED": False,
            "AI_PROVIDER_CONFIGURED": bool(
                (os.environ.get("AI_AGENT_PROVIDER") or os.environ.get("SOCIAL_AGENT_AI_PROVIDER") or "").strip()
            ),
            "META_CONFIGURED": bool(cfg["configured"]),
            "META_APP_ID_CONFIGURED": bool(cfg.get("app_id")),
            "META_APP_SECRET_CONFIGURED": bool(cfg.get("app_secret_present")),
            "META_REDIRECT_URI_CONFIGURED": bool(cfg.get("redirect_uri")),
            "META_APP_MODE": cfg.get("app_mode"),
            "META_API_VERSION": cfg.get("api_version"),
            "SOCIAL_CREDENTIAL_KEY_CONFIGURED": SocialCredentialCrypto.configured(),
            "META_FACEBOOK_PUBLISHING_ENABLED": False,
            "META_INSTAGRAM_PUBLISHING_ENABLED": False,
            "TELEGRAM_CONFIGURED": True,
            "EXSWAPING_API_CONFIGURED": False,
            "STORAGE_CONFIGURED": True,
        }
    )
