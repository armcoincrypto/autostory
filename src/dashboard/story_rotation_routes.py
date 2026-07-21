"""P10.17+ gated Story Rotation API helpers."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from src.core.database import get_db_context
from src.dashboard.auth_access import dashboard_api_authorized
from src.stories.rotation_audit import (
    build_story_dry_run_plan,
    build_story_rotation_precheck,
)
from src.stories.scheduler_integration import build_story_scheduler_integration_map

story_rotation_api = Blueprint("story_rotation_api", __name__, url_prefix="/api/stories")


@story_rotation_api.before_request
def story_rotation_require_dashboard_auth():
    """Reject before DB, session, or Telegram access."""
    if not dashboard_api_authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    return None


@story_rotation_api.route("/precheck", methods=["POST"])
def story_rotation_precheck():
    """Read-only precheck for Story Rotation. Does not create runs or publish stories."""
    payload = request.get_json(silent=True) or {}
    with get_db_context() as db:
        report = build_story_rotation_precheck(db, payload)
    return jsonify(
        {
            "ok": bool(report.get("ok")),
            "precheck": report,
            "eligible_accounts": report.get("eligible_accounts", []),
            "blocked_accounts": report.get("blocked_accounts", []),
            "mention_candidates": report.get("mentions", {}).get("available", 0),
            "media_ready": bool(report.get("media", {}).get("ok")),
            "cooldown_ok": report.get("counts", {}).get("cooldown_blocked", 0) == 0,
            "live_publish_allowed": bool(report.get("live_publish_allowed")),
            "requires_operator_approval": True,
            "blockers": report.get("live_blockers", []),
            "account_blockers": report.get("account_blockers", {}),
            "live_gate_blockers": report.get("live_gate_blockers", []),
        }
    )


@story_rotation_api.route("/dry-run", methods=["POST"])
def story_rotation_dry_run():
    """Build a no-publish Story Rotation execution plan."""
    payload = request.get_json(silent=True) or {}
    with get_db_context() as db:
        plan = build_story_dry_run_plan(db, payload)
    return jsonify({"ok": bool(plan.get("ok")), "dry_run": True, "plan": plan})


@story_rotation_api.route("/runtime-map", methods=["GET"])
def story_rotation_runtime_map():
    """Return the gated Story Rotation scheduler integration map."""
    return jsonify({"ok": True, "integration": build_story_scheduler_integration_map()})


@story_rotation_api.route("/runs", methods=["POST"])
def create_controlled_story_run_gate():
    """Controlled one-account live story run (account #140)."""
    from src.stories.controlled_live_run import controlled_live_run_http_response

    return controlled_live_run_http_response(request.get_json(silent=True) or {})


@story_rotation_api.route("/readiness-preview", methods=["GET"])
def story_readiness_preview():
    """Read-only unified story readiness summary (P10.22). No Telegram calls."""
    try:
        from src.stories.story_readiness_resolver import build_story_readiness_preview
    except ImportError:
        return jsonify(
            {
                "ok": False,
                "error": "story_readiness_feature_unavailable",
            }
        ), 503
    include_protected = request.args.get("include_protected", "").lower() in ("1", "true", "yes")
    account_ids_raw = request.args.get("account_ids")
    account_ids = None
    if account_ids_raw:
        account_ids = [int(x) for x in str(account_ids_raw).split(",") if str(x).strip().isdigit()]
    try:
        with get_db_context() as db:
            preview = build_story_readiness_preview(
                db,
                account_ids=account_ids,
                include_protected=include_protected,
            )
    except Exception:
        return jsonify(
            {
                "ok": False,
                "error": "story_readiness_evaluation_failed",
            }
        ), 503
    return jsonify(preview)


@story_rotation_api.route("/eligible-accounts", methods=["GET"])
def story_rotation_eligible_accounts():
    """Read-only eligibility projection for the Stories UI and regression tests."""
    payload = {
        "pool_id": request.args.get("pool_id"),
        "purpose_filter": request.args.get("purpose_filter"),
        "media_path": request.args.get("media_path"),
        "mentions_per_story": request.args.get("mentions_per_story", type=int) or 0,
        "mention_source_chat_id": request.args.get("mention_source_chat_id", type=int),
        "max_accounts": request.args.get("max_accounts", type=int),
    }
    with get_db_context() as db:
        report = build_story_rotation_precheck(db, payload)
    return jsonify(
        {
            "ok": True,
            "accounts": [
                row for row in report.get("accounts", []) if row.get("story_ready")
            ],
            "ready_account_ids": report.get("ready_account_ids", []),
            "counts": report.get("counts", {}),
            "blocker_counts": report.get("blocker_counts", {}),
        }
    )
