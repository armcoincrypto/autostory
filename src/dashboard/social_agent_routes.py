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
    {"id": "publishing", "label": "Publishing Preview", "path": "/social-agent/publishing", "badge": "dry_run"},
    {"id": "calendar", "label": "Calendar", "path": "/social-agent/calendar"},
    {"id": "media", "label": "Media Library", "path": "/social-agent/media"},
    {"id": "accounts", "label": "Social Accounts", "path": "/social-agent/accounts"},
    {"id": "analytics", "label": "Analytics", "path": "/social-agent/analytics", "badge": "architecture"},
    {"id": "comments", "label": "Comments", "path": "/social-agent/comments", "badge": "architecture"},
    {"id": "messages", "label": "Messages", "path": "/social-agent/messages", "badge": "architecture"},
    {"id": "brand", "label": "Brand Knowledge", "path": "/social-agent/brand"},
    {"id": "automations", "label": "Automations", "path": "/social-agent/automations", "badge": "disabled"},
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
    return _page("social_agent/calendar.html", "calendar")


@social_agent_bp.route("/social-agent/media", methods=["GET"])
def media_page():
    return _page("social_agent/media.html", "media")


@social_agent_bp.route("/social-agent/accounts", methods=["GET"])
def accounts_page():
    return _page("social_agent/accounts.html", "accounts")


@social_agent_bp.route("/social-agent/analytics", methods=["GET"])
def analytics_page():
    return _page("social_agent/analytics.html", "analytics")


@social_agent_bp.route("/social-agent/comments", methods=["GET"])
def comments_page():
    return _page("social_agent/comments.html", "comments")


@social_agent_bp.route("/social-agent/messages", methods=["GET"])
def messages_page():
    return _page("social_agent/messages.html", "messages")


@social_agent_bp.route("/social-agent/brand", methods=["GET"])
def brand_page():
    return _page("social_agent/brand.html", "brand")


@social_agent_bp.route("/social-agent/automations", methods=["GET"])
def automations_page():
    return _page("social_agent/automations.html", "automations")


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


@social_agent_api.route("/content/<int:content_id>/variants/<int:variant_id>", methods=["PATCH"])
def api_patch_variant(content_id: int, variant_id: int):
    data = request.get_json(silent=True) or {}
    from src.social_agent import content_workflow as cw

    with get_db_context() as db:
        out = cw.update_variant(
            db,
            actor=_actor(),
            content_id=content_id,
            variant_id=variant_id,
            body=str(data.get("body") or ""),
        )
        db.commit()
        return jsonify(out), (200 if out.get("ok") else 400)


@social_agent_api.route("/content/<int:content_id>/status", methods=["POST"])
def api_content_status(content_id: int):
    data = request.get_json(silent=True) or {}
    from src.social_agent import content_workflow as cw

    with get_db_context() as db:
        out = cw.transition_status(
            db,
            actor=_actor(),
            content_id=content_id,
            to_status=str(data.get("status") or ""),
            note=data.get("note"),
        )
        db.commit()
        return jsonify(out), (200 if out.get("ok") else 400)


@social_agent_api.route("/content/<int:content_id>/status-events", methods=["GET"])
def api_content_status_events(content_id: int):
    from src.social_agent import content_workflow as cw

    with get_db_context() as db:
        return jsonify(cw.list_status_events(db, content_id))


@social_agent_api.route("/content/<int:content_id>/studio-preview", methods=["GET"])
def api_studio_preview(content_id: int):
    from src.social_agent import content_workflow as cw

    with get_db_context() as db:
        out = cw.studio_previews(db, content_id=content_id)
        return jsonify(out), (200 if out.get("ok") else 404)


@social_agent_api.route("/platforms", methods=["GET"])
def api_platforms():
    from src.social_agent.platforms import platform_catalog

    return jsonify({"ok": True, "platforms": platform_catalog()})


@social_agent_api.route("/media/assets", methods=["GET"])
def api_media_list():
    from src.social_agent import media_library as media

    folder_id = request.args.get("folder_id")
    with get_db_context() as db:
        return jsonify(
            media.list_assets(
                db,
                folder_id=int(folder_id) if folder_id not in (None, "") else None,
            )
        )


@social_agent_api.route("/media/assets", methods=["POST"])
def api_media_upload():
    from src.social_agent import media_library as media

    if "file" not in request.files:
        return jsonify({"ok": False, "error": "file_required"}), 400
    f = request.files["file"]
    folder_id = request.form.get("folder_id")
    with get_db_context() as db:
        out = media.upload_asset(
            db,
            actor=_actor(),
            filename=f.filename or "upload.bin",
            stream=f.stream,
            mime_type=f.mimetype,
            folder_id=int(folder_id) if folder_id not in (None, "") else None,
        )
        db.commit()
        return jsonify(out), (200 if out.get("ok") else 400)


@social_agent_api.route("/media/assets/<int:asset_id>", methods=["DELETE"])
def api_media_delete(asset_id: int):
    from src.social_agent import media_library as media

    with get_db_context() as db:
        out = media.soft_delete_asset(db, actor=_actor(), asset_id=asset_id)
        db.commit()
        return jsonify(out), (200 if out.get("ok") else 400)


@social_agent_api.route("/media/folders", methods=["GET"])
def api_media_folders():
    from src.social_agent import media_library as media

    with get_db_context() as db:
        return jsonify(media.list_folders(db))


@social_agent_api.route("/media/folders", methods=["POST"])
def api_media_create_folder():
    from src.social_agent import media_library as media

    data = request.get_json(silent=True) or {}
    with get_db_context() as db:
        out = media.create_folder(
            db,
            actor=_actor(),
            name=str(data.get("name") or ""),
            parent_id=int(data["parent_id"]) if data.get("parent_id") is not None else None,
        )
        db.commit()
        return jsonify(out), (200 if out.get("ok") else 400)


@social_agent_api.route("/calendar", methods=["GET"])
def api_calendar():
    from src.social_agent import calendar_service as cal

    with get_db_context() as db:
        return jsonify(
            cal.list_entries(
                db,
                view=str(request.args.get("view") or "month"),
                anchor=request.args.get("anchor"),
            )
        )


@social_agent_api.route("/calendar", methods=["POST"])
def api_calendar_create():
    from src.social_agent import calendar_service as cal

    data = request.get_json(silent=True) or {}
    with get_db_context() as db:
        out = cal.create_entry(
            db,
            actor=_actor(),
            content_id=int(data.get("content_id") or 0),
            platform=str(data.get("platform") or "facebook"),
            scheduled_for=str(data.get("scheduled_for") or ""),
            timezone_name=str(data.get("timezone") or "UTC"),
            notes=data.get("notes"),
        )
        db.commit()
        return jsonify(out), (200 if out.get("ok") else 400)


@social_agent_api.route("/calendar/<int:entry_id>", methods=["PATCH"])
def api_calendar_move(entry_id: int):
    from src.social_agent import calendar_service as cal

    data = request.get_json(silent=True) or {}
    with get_db_context() as db:
        out = cal.move_entry(
            db,
            actor=_actor(),
            entry_id=entry_id,
            scheduled_for=str(data.get("scheduled_for") or ""),
        )
        db.commit()
        return jsonify(out), (200 if out.get("ok") else 400)


@social_agent_api.route("/calendar/<int:entry_id>", methods=["DELETE"])
def api_calendar_cancel(entry_id: int):
    from src.social_agent import calendar_service as cal

    with get_db_context() as db:
        out = cal.cancel_entry(db, entry_id=entry_id)
        db.commit()
        return jsonify(out), (200 if out.get("ok") else 400)


@social_agent_api.route("/brand", methods=["GET"])
def api_brand_list():
    from src.social_agent import brand_store as brand

    with get_db_context() as db:
        return jsonify(brand.list_knowledge(db))


@social_agent_api.route("/brand", methods=["POST"])
def api_brand_upsert():
    from src.social_agent import brand_store as brand

    data = request.get_json(silent=True) or {}
    with get_db_context() as db:
        out = brand.upsert_knowledge(
            db,
            actor=_actor(),
            category=str(data.get("category") or ""),
            key=str(data.get("key") or ""),
            title=str(data.get("title") or ""),
            value=str(data.get("value") or ""),
        )
        db.commit()
        return jsonify(out), (200 if out.get("ok") else 400)


@social_agent_api.route("/accounts/providers", methods=["GET"])
def api_accounts_providers():
    from src.social_agent import surfaces

    with get_db_context() as db:
        return jsonify(surfaces.accounts_provider_matrix(db))


@social_agent_api.route("/analytics", methods=["GET"])
def api_analytics():
    from src.social_agent import surfaces

    return jsonify(surfaces.analytics_architecture())


@social_agent_api.route("/comments", methods=["GET"])
def api_comments():
    from src.social_agent import surfaces

    return jsonify(surfaces.comments_architecture())


@social_agent_api.route("/messages", methods=["GET"])
def api_messages():
    from src.social_agent import surfaces

    return jsonify(surfaces.messages_architecture())


@social_agent_api.route("/automations", methods=["GET"])
def api_automations():
    from src.social_agent import surfaces

    with get_db_context() as db:
        return jsonify(surfaces.automations_architecture(db))


@social_agent_api.route("/automations", methods=["POST"])
def api_automations_save():
    from src.social_agent import surfaces

    data = request.get_json(silent=True) or {}
    with get_db_context() as db:
        out = surfaces.save_automation_draft(
            db,
            actor=_actor(),
            name=str(data.get("name") or ""),
            definition=data.get("definition") if isinstance(data.get("definition"), dict) else None,
        )
        db.commit()
        return jsonify(out)


@social_agent_api.route("/publishing/dry-run", methods=["POST"])
def api_publishing_dry_run():
    data = request.get_json(silent=True) or {}
    destinations = list(data.get("destinations") or [])
    content = data.get("content") if isinstance(data.get("content"), dict) else {}
    content_id = data.get("content_id")
    if content_id is not None:
        try:
            content_id = int(content_id)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "content_id_invalid"}), 400
    with get_db_context() as db:
        from src.social_agent.publishing.service import run_publishing_dry_run

        if content_id is not None and not content:
            out = services.publish_preview(
                db,
                actor=_actor(),
                content_id=content_id,
                destinations=destinations or ["facebook_page"],
            )
        else:
            out = run_publishing_dry_run(
                db,
                actor=_actor(),
                destinations=destinations,
                content=content,
                content_id=content_id,
            )
        db.commit()
        return jsonify(out), (200 if out.get("ok") else 400)


@social_agent_api.route("/publishing/history", methods=["GET"])
def api_publishing_history():
    limit = request.args.get("limit", 50)
    try:
        limit_i = int(limit)
    except (TypeError, ValueError):
        limit_i = 50
    with get_db_context() as db:
        from src.social_agent.publishing.service import list_dry_run_history

        return jsonify(list_dry_run_history(db, limit=limit_i))


@social_agent_api.route("/publishing/preview/<int:dry_run_id>", methods=["GET"])
def api_publishing_preview_get(dry_run_id: int):
    with get_db_context() as db:
        from src.social_agent.publishing.service import get_dry_run_preview

        out = get_dry_run_preview(db, dry_run_id)
        return jsonify(out), (200 if out.get("ok") else 404)


@social_agent_api.route("/publishing/preview", methods=["POST"])
def api_publish_preview():
    """Legacy POST preview — delegates to canonical dry-run service."""
    data = request.get_json(silent=True) or {}
    content_id = data.get("content_id")
    destinations = list(data.get("destinations") or ["facebook_page", "instagram_feed"])
    if content_id is None and isinstance(data.get("content"), dict):
        with get_db_context() as db:
            from src.social_agent.publishing.service import run_publishing_dry_run

            out = run_publishing_dry_run(
                db,
                actor=_actor(),
                destinations=destinations,
                content=data.get("content"),
            )
            db.commit()
            return jsonify(out)
    try:
        content_id_i = int(content_id)
    except Exception:
        return jsonify({"ok": False, "error": "content_id_required"}), 400
    with get_db_context() as db:
        out = services.publish_preview(
            db,
            actor=_actor(),
            content_id=content_id_i,
            destinations=destinations,
            content=data.get("content") if isinstance(data.get("content"), dict) else None,
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


@social_agent_api.route("/publishing/facebook-canary/prepare", methods=["POST"])
def api_facebook_canary_prepare():
    with get_db_context() as db:
        from src.social_agent.publishing.publish_service import prepare_facebook_canary_dry_run

        out = prepare_facebook_canary_dry_run(db, actor=_actor())
        db.commit()
        return jsonify(out), (200 if out.get("ok") else 400)


@social_agent_api.route("/publishing/facebook-canary/preflight", methods=["POST"])
def api_facebook_canary_preflight():
    data = request.get_json(silent=True) or {}
    try:
        dry_run_id = int(data["dry_run_id"])
        payload_hash = str(data["payload_hash"])
    except Exception:
        return jsonify({"ok": False, "error": "dry_run_id_and_payload_hash_required"}), 400
    with get_db_context() as db:
        from src.social_agent.publishing.execution import CANARY_FACEBOOK_PAGE_ID
        from src.social_agent.publishing.publish_service import preflight_facebook_canary

        out = preflight_facebook_canary(
            db,
            actor=_actor(),
            workspace_id="default",
            dry_run_id=dry_run_id,
            payload_hash=payload_hash,
            page_id=str(data.get("page_id") or CANARY_FACEBOOK_PAGE_ID),
        )
        db.commit()
        return jsonify(out), (200 if out.get("ok") else 400)


@social_agent_api.route("/publishing/facebook-canary/authorize", methods=["POST"])
def api_facebook_canary_authorize():
    denied = _require_manage_accounts()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    try:
        dry_run_id = int(data["dry_run_id"])
        payload_hash = str(data["payload_hash"])
    except Exception:
        return jsonify({"ok": False, "error": "dry_run_id_and_payload_hash_required"}), 400
    with get_db_context() as db:
        from src.social_agent.publishing.execution import CANARY_FACEBOOK_PAGE_ID
        from src.social_agent.publishing.publish_service import approve_and_mint_canary

        out = approve_and_mint_canary(
            db,
            actor=_actor(),
            workspace_id="default",
            dry_run_id=dry_run_id,
            payload_hash=payload_hash,
            page_id=str(data.get("page_id") or CANARY_FACEBOOK_PAGE_ID),
            explicit_approval=data.get("explicit_approval"),
            idempotency_key=data.get("idempotency_key"),
        )
        db.commit()
        return jsonify(out), (200 if out.get("ok") else 400)


@social_agent_api.route("/publishing/facebook-canary/execute", methods=["POST"])
def api_facebook_canary_execute():
    denied = _require_manage_accounts()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    required = ("authorization_id", "authorization_secret", "dry_run_id", "payload_hash", "idempotency_key")
    if any(k not in data for k in required):
        return jsonify({"ok": False, "error": "missing_required_fields", "required": list(required)}), 400
    with get_db_context() as db:
        from src.social_agent.publishing.execution import CANARY_FACEBOOK_PAGE_ID
        from src.social_agent.publishing.publish_service import execute_facebook_canary

        out = execute_facebook_canary(
            db,
            actor=_actor(),
            workspace_id="default",
            authorization_id=int(data["authorization_id"]),
            authorization_secret=str(data["authorization_secret"]),
            dry_run_id=int(data["dry_run_id"]),
            payload_hash=str(data["payload_hash"]),
            page_id=str(data.get("page_id") or CANARY_FACEBOOK_PAGE_ID),
            idempotency_key=str(data["idempotency_key"]),
        )
        db.commit()
        status = 200 if out.get("ok") else 400
        return jsonify(out), status


@social_agent_api.route("/connections/meta/publish-canary-oauth", methods=["POST"])
def api_meta_publish_canary_oauth():
    """Start OAuth rerequest including pages_manage_posts for the Facebook canary only."""
    denied = _require_manage_accounts()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    with get_db_context() as db:
        from src.social_agent import meta_connection as meta_svc

        out = meta_svc.start_meta_oauth(
            db,
            actor=_actor(),
            purpose="publish_canary",
            connection_id=data.get("connection_id") or 1,
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
