"""Broadcast owner page + API (Wave K fail-closed).

Architecture note:
  Historical Phase 8C UI proxied CRUD/send to AI Factory at
  AI_CODING_API_BASE_URL (default http://127.0.0.1:8015). That upstream is a
  separate codebase (Aicodingauto-) using a bot-token Telegram provider — not
  Storyfleet's certified send rails. Port 8015 is not a running production
  dependency for Storyfleet.

Wave K keeps the owner Broadcast surface but fail-closes all mutations and
does not require :8015 for the page to load.
"""
from __future__ import annotations

import structlog
from flask import Blueprint, jsonify, redirect, render_template, request, url_for
from flask_login import current_user

from .auth_access import dashboard_api_authorized
from .broadcast_guard import broadcast_execution_enabled, broadcast_fail_closed_payload

logger = structlog.get_logger(__name__)

broadcast_bp = Blueprint("broadcast", __name__)
broadcast_api = Blueprint("broadcast_api", __name__, url_prefix="/api/v1/broadcast")


def _actor_label() -> str:
    if current_user.is_authenticated:
        return str(getattr(current_user, "email", None) or getattr(current_user, "id", "dashboard_user"))
    return "admin_token"


def _audit(action: str, **fields) -> None:
    logger.info("broadcast_audit", action=action, actor=_actor_label(), **fields)


def _deny_execution(*, action: str):
    _audit(action, execution_enabled=False, denied=True)
    return jsonify(broadcast_fail_closed_payload()), 403


@broadcast_bp.before_request
def _page_auth():
    if request.endpoint == "broadcast.broadcast_page":
        if not dashboard_api_authorized():
            return redirect(url_for("auth.login", next=request.full_path or request.path))
    return None


@broadcast_api.before_request
def _api_auth():
    if not dashboard_api_authorized():
        return jsonify({"error": "unauthorized"}), 401
    return None


@broadcast_bp.route("/broadcast", methods=["GET"])
def broadcast_page():
    _audit("broadcast_dashboard_viewed", execution_enabled=broadcast_execution_enabled())
    return render_template(
        "broadcast.html",
        execution_enabled=broadcast_execution_enabled(),
        api_base="/api/v1/broadcast",
    )


@broadcast_api.route("/status", methods=["GET"])
def broadcast_status():
    """Cheap static status — no Telegram, no OpenAI, no :8015 call."""
    enabled = broadcast_execution_enabled()
    payload = {
        "ok": True,
        "execution_enabled": enabled,
        "mode": "live" if enabled else "fail_closed",
        "upstream": {
            "classification": "LEGACY_PROXY",
            "default_base": "http://127.0.0.1:8015",
            "note": "AI Factory campaign API — not a Storyfleet certified send rail",
            "required_for_owner_page": False,
        },
        "send_owner": "external_ai_factory_bot_token_provider (uncertified)",
        "owner_copy": (
            "Broadcast live sending is enabled."
            if enabled
            else "Broadcast is not enabled for production. Use Messages for one private reply."
        ),
    }
    _audit("broadcast_status_viewed", execution_enabled=enabled)
    return jsonify(payload)


@broadcast_api.route("/campaigns", methods=["GET", "POST"])
def campaigns_collection():
    if request.method == "POST":
        return _deny_execution(action="broadcast_campaign_create_denied")
    # Fail-closed read: do not depend on :8015. Empty history is honest while
    # the external engine is offline / uncertified.
    _audit("broadcast_campaigns_listed_fail_closed", execution_enabled=False)
    return jsonify([])


@broadcast_api.route("/campaigns/<campaign_id>", methods=["GET", "PATCH"])
def campaign_detail(campaign_id: str):
    if request.method == "PATCH":
        return _deny_execution(action="broadcast_campaign_update_denied")
    return jsonify(
        broadcast_fail_closed_payload(
            detail=f"Broadcast campaign history is unavailable while execution is fail-closed ({campaign_id})."
        )
    ), 404


@broadcast_api.route("/campaigns/<campaign_id>/audience/import", methods=["POST"])
def campaign_audience_import(campaign_id: str):
    return _deny_execution(action="broadcast_audience_import_denied")


@broadcast_api.route("/campaigns/<campaign_id>/audience/preview", methods=["GET"])
def campaign_audience_preview(campaign_id: str):
    return jsonify({"eligible_count": 0, "execution_enabled": False, "campaign_id": campaign_id})


@broadcast_api.route("/campaigns/<campaign_id>/preview", methods=["POST"])
def campaign_message_preview(campaign_id: str):
    return _deny_execution(action="broadcast_message_preview_denied")


@broadcast_api.route("/campaigns/<campaign_id>/dry-run", methods=["POST"])
def campaign_dry_run(campaign_id: str):
    return _deny_execution(action="broadcast_dry_run_denied")


@broadcast_api.route("/suppression", methods=["GET", "POST"])
def suppression_collection():
    if request.method == "POST":
        return _deny_execution(action="broadcast_suppression_add_denied")
    return jsonify([])


@broadcast_api.route("/suppression/<entry_id>", methods=["DELETE"])
def suppression_remove(entry_id: str):
    return _deny_execution(action="broadcast_suppression_remove_denied")


@broadcast_api.route("/campaigns/<campaign_id>/validation", methods=["GET"])
def campaign_validation(campaign_id: str):
    return jsonify(
        {
            "valid": False,
            "execution_enabled": False,
            "blocking": ["broadcast_execution_disabled"],
            "campaign_id": campaign_id,
        }
    )


@broadcast_api.route("/campaigns/<campaign_id>/audit", methods=["GET"])
def campaign_audit(campaign_id: str):
    return jsonify([])


@broadcast_api.route("/campaigns/<campaign_id>/submit-review", methods=["POST"])
def campaign_submit_review(campaign_id: str):
    return _deny_execution(action="broadcast_review_submit_denied")


@broadcast_api.route("/campaigns/<campaign_id>/approve", methods=["POST"])
def campaign_approve(campaign_id: str):
    return _deny_execution(action="broadcast_approve_denied")


@broadcast_api.route("/campaigns/<campaign_id>/reject", methods=["POST"])
def campaign_reject(campaign_id: str):
    return _deny_execution(action="broadcast_reject_denied")


@broadcast_api.route("/campaigns/<campaign_id>/cancel", methods=["POST"])
def campaign_cancel(campaign_id: str):
    return _deny_execution(action="broadcast_cancel_denied")


@broadcast_api.route("/campaigns/<campaign_id>/dry-run/export", methods=["GET"])
def campaign_dry_run_export(campaign_id: str):
    return _deny_execution(action="broadcast_dry_run_export_denied")


@broadcast_api.route("/campaigns/<campaign_id>/send/preview", methods=["GET"])
def campaign_send_preview(campaign_id: str):
    return jsonify(
        {
            "campaign_id": campaign_id,
            "live_send_enabled": False,
            "can_send": False,
            "eligible_count": 0,
            "selected_count": 0,
            "execution_enabled": False,
            "blocking_reasons": ["broadcast_execution_disabled"],
            "confirmation_phrase": "Live sending disabled",
        }
    )


@broadcast_api.route("/campaigns/<campaign_id>/send/tiny-cohort", methods=["POST"])
def campaign_send_tiny_cohort(campaign_id: str):
    # Hard deny even if someone flips a future flag incorrectly without full cert.
    if not broadcast_execution_enabled():
        return _deny_execution(action="broadcast_tiny_cohort_send_denied")
    # Wave K: never enable live send through this proxy without a dedicated cert.
    return _deny_execution(action="broadcast_tiny_cohort_send_uncertified")


@broadcast_api.route("/campaigns/<campaign_id>/send/batches", methods=["GET"])
def campaign_send_batches(campaign_id: str):
    return jsonify([])


@broadcast_api.route("/campaigns/<campaign_id>/send/batches/<batch_id>", methods=["GET"])
def campaign_send_batch_detail(campaign_id: str, batch_id: str):
    return jsonify({"error": "not_found", "campaign_id": campaign_id, "batch_id": batch_id}), 404
