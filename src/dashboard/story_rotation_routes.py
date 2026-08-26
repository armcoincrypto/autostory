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
            "ready_account_ids": report.get("ready_account_ids", []),
            "blocked_accounts": report.get("blocked_accounts", []),
            "mention_candidates": report.get("mentions", {}).get("available", 0),
            "media_ready": bool(report.get("media", {}).get("ok")),
            "cooldown_ok": report.get("counts", {}).get("cooldown_blocked", 0) == 0,
            "live_publish_allowed": bool(report.get("live_publish_allowed")),
            "requires_operator_approval": True,
            "blockers": report.get("live_blockers", []),
            "account_blockers": report.get("account_blockers", {}),
            "live_gate_blockers": report.get("live_gate_blockers", []),
            "mutation_allowlist_account_ids": report.get("mutation_allowlist_account_ids", []),
            "confirmation_token_expected": report.get("confirmation_token_expected"),
            "accounts": report.get("accounts", []),
            "sample_accounts": report.get("sample_accounts", []),
        }
    )


@story_rotation_api.route("/dry-run", methods=["POST"])
def story_rotation_dry_run():
    """Build a no-publish Story Rotation execution plan."""
    payload = request.get_json(silent=True) or {}
    with get_db_context() as db:
        plan = build_story_dry_run_plan(db, payload)
    pre = plan.get("precheck") or {}
    return jsonify(
        {
            "ok": bool(plan.get("ok")),
            "dry_run": True,
            "plan": plan,
            "precheck": pre,
            "selected_accounts": plan.get("selected_accounts", []),
            "selected_mention_candidates": plan.get("selected_mention_candidates", []),
            "per_account_mentions": plan.get("per_account_mentions", []),
            "mutation_allowlist_account_ids": plan.get("mutation_allowlist_account_ids")
            or pre.get("mutation_allowlist_account_ids")
            or [],
            "confirmation_token_expected": plan.get("confirmation_token_expected")
            or pre.get("confirmation_token_expected"),
            "live_publish_allowed": bool(pre.get("live_publish_allowed")),
            "live_gate_blockers": pre.get("live_gate_blockers", []),
            "live_blockers": pre.get("live_blockers", []),
            "execution_order": plan.get("execution_order", []),
        }
    )


@story_rotation_api.route("/runtime-map", methods=["GET"])
def story_rotation_runtime_map():
    """Return the gated Story Rotation scheduler integration map."""
    return jsonify({"ok": True, "integration": build_story_scheduler_integration_map()})


@story_rotation_api.route("/runs", methods=["POST"])
def create_controlled_story_run_gate():
    """Allowlist-gated multi-account controlled live story run."""
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


@story_rotation_api.route("/auto-campaigns/preview", methods=["POST"])
def auto_story_preview():
    from src.stories.auto_story_service import preview_schedule

    payload = request.get_json(silent=True) or {}
    preview = preview_schedule(
        duration_days=int(payload.get("duration_days") or 1),
        posts_per_day=int(payload.get("posts_per_day") or 1),
        times_json=payload.get("times_json"),
        payload=payload,
    )
    return jsonify({"ok": True, **preview})


@story_rotation_api.route("/auto-campaigns/fleet-summary", methods=["GET"])
def auto_story_fleet_summary():
    from src.core.database import get_db_context
    from src.stories.autostory_operator_preview import fleet_autostory_summary
    from src.stories.mutation_boundary import story_mutations_enabled
    from src.stories.scheduler_integration import scheduler_story_execution_enabled

    with get_db_context() as db:
        summary = fleet_autostory_summary(db)
    summary.pop("_eligible_all_ids", None)
    summary["scheduler_active"] = bool(
        story_mutations_enabled() and scheduler_story_execution_enabled()
    )
    summary["story_mutations_enabled"] = story_mutations_enabled()
    summary["scheduler_story_execution_enabled"] = scheduler_story_execution_enabled()
    if summary["scheduler_active"]:
        summary["safety_banner"] = {
            "state": "active",
            "title": "AutoStory Scheduler Active",
            "message": "Only approved campaign waves can publish.",
        }
    else:
        summary["safety_banner"] = {
            "state": "paused",
            "title": "AutoStory Publishing Paused",
            "message": "New campaigns will not publish until the global safety switch is enabled.",
        }
    return jsonify({"ok": True, **summary})


@story_rotation_api.route("/auto-campaigns/<int:campaign_id>/activity", methods=["GET"])
def auto_story_campaign_activity(campaign_id: int):
    from src.core.database import get_db_context
    from src.stories.auto_story_service import get_campaign
    from src.stories.autostory_operator_preview import get_campaign_activity

    camp = get_campaign(campaign_id)
    if not camp:
        return jsonify({"ok": False, "error": "not_found"}), 404
    with get_db_context() as db:
        events = get_campaign_activity(db, campaign_id, limit=int(request.args.get("limit") or 80))
    return jsonify({"ok": True, "campaign_id": campaign_id, "events": events, "campaign": camp})


@story_rotation_api.route("/auto-campaigns", methods=["GET"])
def auto_story_list():
    from src.stories.auto_story_service import get_active_campaigns, get_recent_campaigns

    recent = request.args.get("recent")
    if recent in ("1", "true", "yes"):
        return jsonify({"ok": True, "campaigns": get_recent_campaigns(limit=int(request.args.get("limit") or 25))})
    return jsonify({"ok": True, "campaigns": get_active_campaigns()})


@story_rotation_api.route("/auto-campaigns/active", methods=["GET"])
def auto_story_active():
    from src.stories.auto_story_service import get_active_campaigns

    camps = [c for c in get_active_campaigns() if c.get("status") in ("active", "paused")]
    return jsonify({"ok": True, "campaigns": camps, "campaign": camps[0] if camps else None})


@story_rotation_api.route("/auto-campaigns", methods=["POST"])
def auto_story_create():
    from src.stories.auto_story_service import create_campaign

    result = create_campaign(request.get_json(silent=True) or {})
    status = 200 if result.get("ok") else 400
    return jsonify(result), status


@story_rotation_api.route("/auto-campaigns/policy-sweep", methods=["GET"])
def auto_story_policy_sweep():
    """Read-only sweep of non-terminal campaigns against current execution policy."""
    from src.stories.auto_story_service import get_active_campaigns, get_recent_campaigns
    from src.stories.autostory_media import classify_nonterminal_campaigns

    camps = get_active_campaigns()
    # Also include drafts that may not appear in "active" depending on filter
    recent = get_recent_campaigns(limit=50)
    by_id = {int(c["id"]): c for c in recent if c.get("status") in {"draft", "active", "paused", "scheduled", "approved"}}
    for c in camps:
        by_id[int(c["id"])] = c
    rows = classify_nonterminal_campaigns(list(by_id.values()))
    return jsonify({"ok": True, "campaigns": rows, "count": len(rows)})


@story_rotation_api.route("/auto-campaigns/<int:campaign_id>", methods=["GET"])
def auto_story_get(campaign_id: int):
    from src.stories.auto_story_service import get_campaign

    camp = get_campaign(campaign_id)
    if not camp:
        return jsonify({"ok": False, "error": "not_found"}), 404
    return jsonify({"ok": True, "campaign": camp})


@story_rotation_api.route("/auto-campaigns/<int:campaign_id>/dry-run", methods=["POST"])
def auto_story_dry_run(campaign_id: int):
    from src.stories.auto_story_service import dry_run_campaign_wave

    result = dry_run_campaign_wave(campaign_id)
    status = 200 if result.get("ok") or result.get("error") != "not_found" else 404
    if result.get("error") == "not_found":
        return jsonify(result), 404
    return jsonify(result), status


@story_rotation_api.route("/auto-campaigns/<int:campaign_id>/activate", methods=["POST"])
def auto_story_activate(campaign_id: int):
    from src.stories.auto_story_service import activate_campaign

    result = activate_campaign(campaign_id, request.get_json(silent=True) or {})
    status = 200 if result.get("ok") else 400
    if result.get("error") == "not_found":
        status = 404
    return jsonify(result), status


@story_rotation_api.route("/auto-campaigns/<int:campaign_id>/run-now", methods=["POST"])
def auto_story_run_now(campaign_id: int):
    from src.stories.auto_story_service import activate_campaign, execute_wave, get_campaign

    payload = request.get_json(silent=True) or {}
    camp = get_campaign(campaign_id)
    if not camp:
        return jsonify({"ok": False, "error": "not_found"}), 404
    if camp.get("status") != "active":
        act = activate_campaign(campaign_id, payload)
        if not act.get("ok"):
            return jsonify(act), 400
    result = execute_wave(campaign_id, operator_manual=True, require_scheduler_flag=False)
    status = 200 if result.get("ok") else 409
    if result.get("error") in ("story_mutations_disabled", "controlled_story_execution_disabled"):
        status = 403
    return jsonify(result), status


@story_rotation_api.route("/auto-campaigns/<int:campaign_id>/pause", methods=["POST"])
def auto_story_pause(campaign_id: int):
    from src.stories.auto_story_service import pause_campaign

    result = pause_campaign(campaign_id)
    status = 200 if result.get("ok") else 400
    if result.get("error") == "not_found":
        status = 404
    return jsonify(result), status


@story_rotation_api.route("/auto-campaigns/<int:campaign_id>/cancel", methods=["POST"])
def auto_story_cancel(campaign_id: int):
    from src.stories.auto_story_service import cancel_campaign

    result = cancel_campaign(campaign_id)
    status = 200 if result.get("ok") else 400
    if result.get("error") == "not_found":
        status = 404
    return jsonify(result), status
