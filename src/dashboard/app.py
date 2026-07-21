"""
Flask Application Factory
STORYFLEET Control Dashboard
"""
import importlib
import os
import sys
import threading
import time
import traceback
from collections import defaultdict, deque
from datetime import timezone
from types import SimpleNamespace
from typing import Any

from flask import Flask, jsonify, make_response, request
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect
from flask_wtf.csrf import CSRFError
from werkzeug.middleware.proxy_fix import ProxyFix
import structlog

# Add project root to path (works for both /home/user/autostory and /opt/autostory)
_script_dir = os.path.dirname(os.path.abspath(__file__))
_project_root = os.path.abspath(os.path.join(_script_dir, "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from config.settings import settings

logger = structlog.get_logger(__name__)

login_manager = LoginManager()
csrf = CSRFProtect()
_diagnostic_rate_lock = threading.Lock()
_diagnostic_rate_events: dict[str, deque[float]] = defaultdict(deque)
_DIAGNOSTIC_RATE_WINDOW_SEC = 60.0
_DIAGNOSTIC_RATE_MAX = 30


def _production_environment() -> bool:
    return settings.environment.strip().lower() in ("production", "prod")


def _load_optional_blueprints(
    app: Flask,
    module_name: str,
    attribute_names: tuple[str, ...],
) -> list[Any]:
    """Register an optional dashboard feature only when its source is complete."""
    try:
        module = importlib.import_module(module_name)
        blueprints = [getattr(module, name) for name in attribute_names]
    except Exception as exc:
        logger.warning(
            "optional_dashboard_feature_unavailable",
            module=module_name,
            error_class=type(exc).__name__,
            error=str(exc),
        )
        return []
    for blueprint in blueprints:
        app.register_blueprint(blueprint)
    return blueprints


def _configure_reverse_proxy_and_session(app: Flask) -> None:
    """
    Trust nginx/Cloudflare forwarded headers and align session cookies with HTTPS.

    Without ProxyFix, ``request.is_secure`` stays false behind TLS termination while
    Flask-WTF ``WTF_CSRF_SSL_STRICT`` still enforces referrer checks when the proxy
    sets ``X-Forwarded-Proto: https``. Session cookies must use ``Secure`` on HTTPS.
    """
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    if _production_environment():
        app.config["PREFERRED_URL_SCHEME"] = "https"
        app.config["SESSION_COOKIE_SECURE"] = True


def _ensure_operational_state_p9_handler(app: Flask) -> None:
    """
    P9.13 — Legacy ``routes.pyc`` registers duplicate ``/api/accounts/operational-state``.

    Force both endpoints to use the P9.1 view (feature-flagged 404 when disabled).
    """
    p9_key = "operational_state_api.accounts_operational_state"
    if p9_key not in app.view_functions:
        return
    p9_view = app.view_functions[p9_key]
    for legacy_key in ("api.get_operational_state",):
        if legacy_key in app.view_functions:
            app.view_functions[legacy_key] = p9_view
            logger.info(
                "operational_state_route_patched",
                legacy_endpoint=legacy_key,
                handler=p9_key,
            )


def _ensure_legacy_api_guards(app: Flask) -> None:
    """Patch bytecode-only legacy API views with P10.16 JSON/safety guards."""
    scan_key = "api.scan_channel"
    legacy_scan = app.view_functions.get(scan_key)
    if legacy_scan is not None and not getattr(legacy_scan, "_p10_16_guarded", False):

        def scan_channel_guarded(*args, **kwargs):
            data = request.get_json(silent=True) or {}
            channels_raw = data.get("channels", [])
            if isinstance(channels_raw, str):
                channels_raw = channels_raw.splitlines()
            if not isinstance(channels_raw, list):
                return jsonify({"ok": False, "success": False, "error": "channels must be a list or newline-separated string"}), 400
            channels = [str(c).strip() for c in channels_raw if str(c or "").strip()]
            if not channels:
                return jsonify(
                    {
                        "ok": False,
                        "success": False,
                        "error": "channels list required (e.g. @p2pgroup or one per line)",
                    }
                ), 400
            if data.get("dry_run") is True:
                return jsonify(
                    {
                        "ok": True,
                        "success": True,
                        "dry_run": True,
                        "channels": channels,
                        "total_new_users": 0,
                        "message": "Dry-run validation only; scan was not started.",
                    }
                )
            return legacy_scan(*args, **kwargs)

        scan_channel_guarded._p10_16_guarded = True
        app.view_functions[scan_key] = scan_channel_guarded
        logger.info("legacy_api_guard_installed", endpoint=scan_key)


def _p10_17_dt_iso_optional(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        return value or None
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        try:
            return iso()
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _p10_17_status_value(status: Any) -> str:
    if status is None:
        return "inactive"
    return str(getattr(status, "value", status) or "inactive")


def _p10_17_general_health(account: Any) -> dict[str, str]:
    status = _p10_17_status_value(getattr(account, "status", None)).strip().lower()
    health_raw = getattr(account, "health_status", None)
    health = str(health_raw or "").strip().lower()
    reason = str(getattr(account, "health_reason", None) or "").strip()
    if status != "active":
        return {
            "general_health_label": "Inactive / auth required",
            "general_health_reason": reason or f"Account status={status}.",
        }
    if health == "alive":
        return {"general_health_label": "Alive", "general_health_reason": reason or "Last general healthcheck: alive."}
    if health in ("frozen", "restricted"):
        return {"general_health_label": "Frozen", "general_health_reason": reason or f"General healthcheck: {health}."}
    if health in ("auth_required", "deleted", "banned"):
        return {
            "general_health_label": "Inactive / auth required",
            "general_health_reason": reason or f"General healthcheck: {health}.",
        }
    if health == "flood_wait":
        return {"general_health_label": "Flood Wait", "general_health_reason": reason or "General healthcheck: flood wait."}
    return {"general_health_label": "Unknown", "general_health_reason": reason or "No recent general healthcheck."}


def _p10_17_accounts_summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    summary = {
        "total": len(rows),
        "active": 0,
        "auth_required": 0,
        "flood_wait": 0,
        "frozen": 0,
        "banned": 0,
        "other": 0,
        "story_available": 0,
        "story_ok": 0,
        "story_frozen": 0,
        "story_rate_limited": 0,
        "canonical_session_ready": 0,
        "missing_canonical_session": 0,
        "general_healthy": 0,
        "warmup_pending": 0,
    }
    for row in rows:
        status = str(row.get("status") or "").lower()
        health = str(row.get("health_status") or "").lower()
        if health == "alive":
            summary["general_healthy"] += 1
        if health == "alive" or status == "active":
            summary["active"] += 1
        elif status == "auth_required" or health == "auth_required":
            summary["auth_required"] += 1
        elif status == "flood_wait" or health == "flood_wait":
            summary["flood_wait"] += 1
        elif health in ("frozen", "restricted") or status == "frozen":
            summary["frozen"] += 1
        elif status == "banned" or health == "banned":
            summary["banned"] += 1
        else:
            summary["other"] += 1
        if row.get("is_story_ready"):
            summary["story_available"] += 1
        story_ui = row.get("story_ui_status")
        if story_ui == "ready":
            summary["story_ok"] += 1
        elif story_ui in ("frozen", "restricted", "telegram_denied", "blocked"):
            summary["story_frozen"] += 1
        elif story_ui == "rate_limited":
            summary["story_rate_limited"] += 1
        if row.get("session_readiness") == "canonical_ok":
            summary["canonical_session_ready"] += 1
        elif row.get("session_readiness") in ("missing_canonical", "needs_reimport"):
            summary["missing_canonical_session"] += 1
        if row.get("warmup_status") in ("new", "warming") and row.get("warmup_block_reason"):
            summary["warmup_pending"] += 1
    return summary


def _ensure_p10_17_accounts_api(app: Flask) -> None:
    """Replace fragile bytecode-only /api/accounts with a per-field guarded DB projection."""
    endpoint = "api.list_accounts"
    if endpoint not in app.view_functions or getattr(app.view_functions[endpoint], "_p10_17_guarded", False):
        return

    def list_accounts_guarded():
        from src.core.database import get_db_context
        from src.core.models import Account
        from src.core.session_paths import (
            account_has_canonical_session,
            get_existing_canonical_account_ids,
            get_session_readiness,
            get_story_availability,
        )
        from src.core.account_protection import PROTECTED_IDS, PURPOSE_HOLD_IDS
        from src.core.safety_policy import get_account_risk_level, get_story_safety_decision
        from src.ai_agent.account_allowlist import RESERVED_AI_AGENT_ACCOUNT_IDS

        limit = min(500, max(1, request.args.get("limit", 100, type=int)))
        purpose_filter = (request.args.get("purpose") or "").strip().lower()
        want_summary = request.args.get("summary", "").strip().lower() in ("1", "true", "yes")
        canonical_exists = get_existing_canonical_account_ids()
        rows: list[dict[str, Any]] = []
        with get_db_context() as db:
            accounts = db.query(Account).order_by(Account.id).limit(limit).all()
            for account in accounts:
                purpose = (getattr(account, "purpose", None) or "both").strip().lower()
                if purpose_filter == "messaging" and purpose == "autostory":
                    continue
                if purpose_filter == "autostory" and purpose == "messaging":
                    continue
                row: dict[str, Any] = {
                    "id": account.id,
                    "phone_number": account.phone_number,
                    "username": account.username,
                    "first_name": account.first_name,
                    "status": _p10_17_status_value(account.status),
                    "purpose": purpose,
                    "last_active": _p10_17_dt_iso_optional(getattr(account, "last_active", None)),
                    "stories_today": int(getattr(account, "stories_today", None) or 0),
                    "session_path": getattr(account, "session_path", None),
                    "health_status": getattr(account, "health_status", None),
                    "health_reason": getattr(account, "health_reason", None),
                    "health_checked_at": _p10_17_dt_iso_optional(getattr(account, "health_checked_at", None)),
                    "story_status": getattr(account, "story_status", None),
                    "story_status_reason": getattr(account, "story_status_reason", None),
                    "story_status_checked_at": _p10_17_dt_iso_optional(getattr(account, "story_status_checked_at", None)),
                    "story_blocked_until": _p10_17_dt_iso_optional(getattr(account, "story_blocked_until", None)),
                    "story_precheck_status": getattr(account, "story_precheck_status", None),
                    "story_precheck_reason": getattr(account, "story_precheck_reason", None),
                    "story_precheck_checked_at": _p10_17_dt_iso_optional(getattr(account, "story_precheck_checked_at", None)),
                    "profile_capability_status": getattr(account, "profile_capability_status", None),
                    "profile_capability_reason": getattr(account, "profile_capability_reason", None),
                    "imported_at": _p10_17_dt_iso_optional(getattr(account, "imported_at", None)),
                    "last_story_attempt_at": _p10_17_dt_iso_optional(getattr(account, "last_story_attempt_at", None)),
                    "successful_story_count": int(getattr(account, "successful_story_count", None) or 0),
                    "failed_story_count": int(getattr(account, "failed_story_count", None) or 0),
                    "manual_review_required": bool(getattr(account, "manual_review_required", False)),
                    "protected_or_held": account.id in PROTECTED_IDS or account.id in PURPOSE_HOLD_IDS,
                    "ai_agent_reserved": account.id in RESERVED_AI_AGENT_ACCOUNT_IDS,
                }
                row["has_session"] = account_has_canonical_session(account, _canonical_exists=canonical_exists)
                row["session_readiness"] = get_session_readiness(account, _canonical_exists=canonical_exists)
                try:
                    story_avail = get_story_availability(account, _canonical_exists=canonical_exists)
                except Exception as exc:
                    logger.warning("p10_17_story_availability_failed", account_id=account.id, error=str(exc))
                    story_avail = {
                        "story_ui_status": "unknown",
                        "story_available_label": "Unknown",
                        "story_reason": "Story readiness calculation failed.",
                        "is_story_ready": False,
                        "story_precheck_stale": True,
                    }
                row.update(
                    {
                        "story_ui_status": story_avail.get("story_ui_status"),
                        "story_available_label": story_avail.get("story_available_label"),
                        "story_reason": story_avail.get("story_reason"),
                        "is_story_ready": bool(story_avail.get("is_story_ready")),
                        "story_precheck_stale": bool(story_avail.get("story_precheck_stale")),
                    }
                )
                row["story_precheck_queue_detail"] = (
                    "precheck_never_run"
                    if row["story_ui_status"] == "needs_precheck" and getattr(account, "story_precheck_checked_at", None) is None
                    else ("precheck_post_ttl_expired" if row["story_ui_status"] == "needs_precheck" else None)
                )
                try:
                    from src.core.warmup import format_warmup_for_ui

                    warmup = format_warmup_for_ui(account)
                except Exception:
                    warmup = {}
                row["warmup_status"] = warmup.get("warmup_status") or getattr(account, "warmup_status", None)
                row["warmup_label"] = warmup.get("warmup_label")
                row["warmup_block_reason"] = warmup.get("warmup_block_reason")
                try:
                    decision = get_story_safety_decision(
                        account, requested_action="story_publish", _canonical_exists=canonical_exists
                    )
                    row["risk_level"] = get_account_risk_level(account)
                    row["story_safety_allowed"] = bool(decision.allowed)
                    row["story_safety_reason"] = decision.reason_code
                    row["story_safety_human_reason"] = decision.human_reason
                    row["operator_action"] = decision.operator_action or None
                    row["can_publish_story_now"] = bool(decision.allowed)
                    row["publish_story_status"] = "yes" if decision.allowed else "no"
                    row["publish_story_reason"] = decision.human_reason or ""
                    row["publish_story_next_allowed_at"] = _p10_17_dt_iso_optional(decision.next_allowed_at)
                    row["publish_story_operator_action"] = decision.operator_action or None
                    row["publish_story_reason_code"] = decision.reason_code or None
                    if row["protected_or_held"] or row["ai_agent_reserved"]:
                        row["story_safety_allowed"] = False
                        row["can_publish_story_now"] = False
                        row["publish_story_status"] = "no"
                        row["publish_story_reason_code"] = (
                            "protected_or_held" if row["protected_or_held"] else "ai_agent_reserved"
                        )
                        row["publish_story_reason"] = (
                            "Protected/held account is excluded from Story Rotation."
                            if row["protected_or_held"]
                            else "AI Agent reserved account is excluded from Story Rotation."
                        )
                        row["publish_story_operator_action"] = "Choose a non-protected fleet account"
                except Exception as exc:
                    logger.warning("p10_17_story_safety_failed", account_id=account.id, error=str(exc))
                    row["risk_level"] = None
                    row["story_safety_allowed"] = False
                    row["story_safety_reason"] = "story_safety_error"
                    row["story_safety_human_reason"] = "Story safety calculation failed."
                    row["operator_action"] = "Review account"
                    row["can_publish_story_now"] = False
                    row["publish_story_status"] = "no"
                    row["publish_story_reason"] = "Story safety calculation failed."
                    row["publish_story_next_allowed_at"] = None
                    row["publish_story_operator_action"] = "Review account"
                    row["publish_story_reason_code"] = "story_safety_error"
                row.update(_p10_17_general_health(SimpleNamespace(**row)))
                rows.append(row)
        if want_summary:
            return jsonify({"accounts": rows, "summary": _p10_17_accounts_summary(rows)})
        return jsonify(rows)

    list_accounts_guarded._p10_17_guarded = True
    app.view_functions[endpoint] = list_accounts_guarded
    logger.info("p10_17_accounts_api_guard_installed", endpoint=endpoint)


def _ensure_p10_17_story_precheck_compat(app: Flask) -> None:
    """Patch bytecode-only story precheck helpers for JSON/test compatibility."""
    import sys as _sys

    routes_mod = _sys.modules.get("src.dashboard.routes")

    candidates_endpoint = "api.story_precheck_candidates"
    if candidates_endpoint in app.view_functions and not getattr(
        app.view_functions[candidates_endpoint], "_p10_17_guarded", False
    ):

        def story_precheck_candidates_guarded():
            from src.core.models import Account
            from src.core.session_paths import (
                account_has_canonical_session,
                get_existing_canonical_account_ids,
                get_story_availability,
            )

            limit = min(500, max(1, request.args.get("limit", 100, type=int)))
            canonical_exists = get_existing_canonical_account_ids()
            get_db_context_fn = getattr(routes_mod, "get_db_context", None)
            if get_db_context_fn is None:
                from src.core.database import get_db_context as get_db_context_fn

            account_ids: list[int] = []
            with get_db_context_fn() as db:
                accounts = db.query(Account).order_by(Account.id).all()
                for account in accounts:
                    if len(account_ids) >= limit:
                        break
                    status = _p10_17_status_value(getattr(account, "status", None))
                    purpose = (getattr(account, "purpose", None) or "both").strip().lower()
                    if status != "active" or purpose == "ai_agent":
                        continue
                    if not account_has_canonical_session(account, _canonical_exists=canonical_exists):
                        continue
                    story_avail = get_story_availability(account, _canonical_exists=canonical_exists)
                    if story_avail.get("story_ui_status") == "needs_precheck":
                        account_ids.append(int(account.id))
            return jsonify(
                {
                    "success": True,
                    "account_ids": account_ids,
                    "count": len(account_ids),
                    "nothing_to_run": len(account_ids) == 0,
                    "remaining_capacity": max(0, 20 - len(account_ids)),
                }
            )

        story_precheck_candidates_guarded._p10_17_guarded = True
        app.view_functions[candidates_endpoint] = story_precheck_candidates_guarded
        logger.info("p10_17_story_precheck_candidates_guard_installed", endpoint=candidates_endpoint)

    precheck_endpoint = "api.run_story_precheck"
    legacy_precheck = app.view_functions.get(precheck_endpoint)
    if legacy_precheck is not None and not getattr(legacy_precheck, "_p10_17_guarded", False):

        def run_story_precheck_guarded(*args, **kwargs):
            data = request.get_json(silent=True) or {}
            account_ids = data.get("account_ids") or []
            if not isinstance(account_ids, list):
                account_ids = [account_ids]
            account_ids = [int(x) for x in account_ids if x is not None]
            try:
                from src.core.risk_events import count_events_last_hour

                used = int(count_events_last_hour("story_precheck") or 0)
            except Exception:
                used = 0
            cap = 20
            remaining = max(0, cap - used)
            if used >= cap:
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": "Story precheck hourly cap reached.",
                            "processed_count": 0,
                            "nothing_processed": True,
                            "remaining_capacity": 0,
                        }
                    ),
                    429,
                )
            if len(account_ids) > remaining:
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": f"Story precheck would exceed hourly cap; remaining capacity is {remaining}.",
                            "processed_count": 0,
                            "nothing_processed": True,
                            "remaining_capacity": remaining,
                        }
                    ),
                    429,
                )
            return legacy_precheck(*args, **kwargs)

        run_story_precheck_guarded._p10_17_guarded = True
        app.view_functions[precheck_endpoint] = run_story_precheck_guarded
        logger.info("p10_17_story_precheck_guard_installed", endpoint=precheck_endpoint)


def _ensure_p10_19_story_run_gate(app: Flask) -> None:
    """Intercept POST /api/stories/runs with the P4B controlled live gate."""
    if getattr(app, "_p10_19_story_run_gate_installed", False):
        return

    @app.before_request
    def p10_19_story_run_gate():
        if request.path != "/api/stories/runs" or request.method != "POST":
            return None
        from src.stories.controlled_live_run import controlled_live_run_http_response

        return controlled_live_run_http_response(request.get_json(silent=True) or {})

    app._p10_19_story_run_gate_installed = True


_LOCKED_STORY_PUBLISH_PATHS = frozenset({"/api/stories/publish", "/api/stories/batch"})


def _ensure_p3_discovery_api_guards(app: Flask) -> None:
    """Defense in depth for bytecode discovery routes (scan/join)."""
    if getattr(app, "_p3_discovery_api_guards_installed", False):
        return

    @app.before_request
    def p3_discovery_api_guard():
        path = (request.path or "").rstrip("/")
        if request.method != "POST":
            return None
        action = None
        if path == "/api/discovery/scan":
            action = "discovery_scan"
        elif path == "/api/discovery/join":
            action = "discovery_join"
        if not action:
            return None
        from src.core.execution_guard import can_execute_action

        data = request.get_json(silent=True) or {}
        dry_run = bool(data.get("dry_run"))
        decision = can_execute_action(
            action,
            account_id=data.get("account_id"),
            dry_run=dry_run,
        )
        if not decision.allowed:
            logger.info(
                "p3_discovery_blocked",
                path=path,
                reason=decision.reason_code,
                dry_run=dry_run,
            )
            return (
                jsonify(
                    {
                        "ok": False,
                        "success": False,
                        "error": decision.reason_code,
                        "message": decision.message,
                        "execution_guard": decision.to_dict(),
                    }
                ),
                403,
            )
        if dry_run:
            target = data.get("channels") or data.get("channel") or data.get("username")
            return jsonify(
                {
                    "ok": True,
                    "success": True,
                    "dry_run": True,
                    "action": action,
                    "target": target,
                    "message": "Dry-run validation only; discovery was not started.",
                }
            )
        return None

    app._p3_discovery_api_guards_installed = True


def _ensure_p3_deep_health_route(app: Flask) -> None:
    """GET /api/health/deep — restricted lock, queue, and emergency status."""
    if "api_health_deep" in app.view_functions:
        return

    @app.route("/api/health/deep", methods=["GET"], endpoint="api_health_deep")
    def api_health_deep():
        from src.dashboard.auth_access import operator_api_authorized

        if not operator_api_authorized():
            response = make_response(
                jsonify({"ok": False, "error": "unauthorized"}), 401
            )
            response.headers["Cache-Control"] = "no-store"
            return response

        now = time.monotonic()
        client_key = request.remote_addr or "unknown"
        with _diagnostic_rate_lock:
            events = _diagnostic_rate_events[client_key]
            while events and now - events[0] >= _DIAGNOSTIC_RATE_WINDOW_SEC:
                events.popleft()
            if len(events) >= _DIAGNOSTIC_RATE_MAX:
                response = make_response(
                    jsonify({"ok": False, "error": "rate_limited"}), 429
                )
                response.headers["Cache-Control"] = "no-store"
                response.headers["Retry-After"] = "60"
                return response
            events.append(now)

        queue: dict[str, int] = {}
        running_stale = 0
        emergency_lock = None
        lock_snapshot: dict[str, Any] = {}
        execution_lock_matrix: dict[str, Any] = {}
        try:
            from datetime import datetime

            from sqlalchemy import func
            from src.core.database import get_db_context
            from src.core.execution_guard import (
                build_execution_lock_matrix,
                execution_emergency_lock_active,
            )
            from src.core.scheduler_models import JobStatus, ScheduledJob
            from src.recovery.p9_83_governance_observability import build_lock_snapshot

            with get_db_context() as db:
                rows = (
                    db.query(ScheduledJob.status, func.count(ScheduledJob.id))
                    .group_by(ScheduledJob.status)
                    .all()
                )
                queue = {str(s): int(c) for s, c in rows}
                running_stale = (
                    db.query(ScheduledJob)
                    .filter(
                        ScheduledJob.status == JobStatus.RUNNING.value,
                        ScheduledJob.lease_until.isnot(None),
                        ScheduledJob.lease_until < datetime.utcnow(),
                    )
                    .count()
                )
            emergency_lock = execution_emergency_lock_active()
            lock_snapshot = build_lock_snapshot()
            execution_lock_matrix = build_execution_lock_matrix()
        except Exception as exc:
            logger.warning(
                "deep_health_queue_query_failed",
                error_type=type(exc).__name__,
            )
            queue = {"_error": 1}
            running_stale = -1

        body = {
            "service": "storyfleet",
            "status": "healthy" if running_stale >= 0 else "degraded",
            "deep": True,
            "execution_emergency_lock": emergency_lock,
            "lock_snapshot": lock_snapshot,
            "execution_lock_matrix": execution_lock_matrix,
            "scheduled_jobs_by_status": queue,
            "stale_running_jobs": int(running_stale),
        }
        if running_stale < 0:
            body["queue_error"] = "dependency_query_failed"
        response = make_response(jsonify(body), 200)
        response.headers["Cache-Control"] = "no-store"
        return response

    logger.info("p3_deep_health_route_installed")


def _ensure_p3_story_publish_gate(app: Flask) -> None:
    """Always block legacy bytecode publish/batch; controlled live uses POST /api/stories/runs only."""
    if getattr(app, "_p3_story_publish_gate_installed", False):
        return

    @app.before_request
    def p3_story_publish_gate():
        if request.method != "POST":
            return None
        path = (request.path or "").rstrip("/")
        if path not in _LOCKED_STORY_PUBLISH_PATHS:
            return None
        logger.info("p3_story_publish_blocked", path=path, method=request.method)
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "story_execution_disabled",
                    "message": "Story publishing is locked. Use dry-run and controlled story runs only.",
                }
            ),
            403,
        )

    app._p3_story_publish_gate_installed = True
    logger.info("p3_story_publish_guard_installed", paths=sorted(_LOCKED_STORY_PUBLISH_PATHS))


def _ensure_dexpert_audit_route(app: Flask) -> None:
    """Register read-only Dexpert audit route when legacy route loading is partial."""
    if any(rule.rule == "/dexpert" for rule in app.url_map.iter_rules()):
        return
    try:
        from .dexpert_routes import register_dexpert_audit

        register_dexpert_audit(app)
        logger.info("dexpert_audit_route_registered_direct")
    except Exception as e:
        logger.warning("dexpert_audit_route_register_failed", error=str(e))
        from flask import Response
        from flask_login import login_required

        @app.route("/dexpert", endpoint="dexpert_audit_fallback")
        @login_required
        def dexpert_audit_fallback():
            return Response(
                """
                <!doctype html>
                <html><head><title>Dexpert - STORYFLEET</title></head>
                <body style="background:#0f172a;color:#e5e7eb;font-family:Inter,system-ui,sans-serif;padding:32px">
                  <section style="max-width:920px;margin:auto;border:1px solid rgba(148,163,184,.35);border-radius:24px;padding:28px;background:linear-gradient(135deg,rgba(30,41,59,.92),rgba(15,23,42,.92))">
                    <p style="letter-spacing:.18em;text-transform:uppercase;color:#a78bfa;font-size:12px">Dexpert Runtime Audit</p>
                    <h1 style="font-size:30px;margin:0 0 12px">Read-only fallback is live</h1>
                    <p style="color:#cbd5e1;line-height:1.6">
                      The full Dexpert audit route is waiting on the Kathleen bridge dependency.
                      This fallback confirms the operator route is reachable without enabling
                      mutations, messaging, autonomous campaigns, or Telegram joins.
                    </p>
                    <div style="display:inline-block;margin-top:12px;border-radius:999px;padding:8px 12px;background:rgba(245,158,11,.16);color:#fbbf24;border:1px solid rgba(245,158,11,.35)">
                      Runtime state: locked / read-only
                    </div>
                  </section>
                </body></html>
                """,
                mimetype="text/html",
            )


def _import_dashboard_routes():
    """Load legacy dashboard routes (``.py`` or bytecode-only after source drift)."""
    try:
        from . import routes as routes_mod

        return routes_mod.register_routes, routes_mod.api
    except (ModuleNotFoundError, ImportError):
        import importlib.util
        from pathlib import Path

        pyc = (
            Path(__file__).parent
            / "__pycache__"
            / f"routes.{sys.implementation.cache_tag}.pyc"
        )
        if not pyc.is_file():
            logger.warning("dashboard_routes_missing", pyc=str(pyc))
            return None, None
        spec = importlib.util.spec_from_file_location("src.dashboard.routes", pyc)
        if spec is None or spec.loader is None:
            logger.warning("dashboard_routes_spec_failed", pyc=str(pyc))
            return None, None
        routes_mod = importlib.util.module_from_spec(spec)
        sys.modules["src.dashboard.routes"] = routes_mod
        spec.loader.exec_module(routes_mod)
        if not hasattr(routes_mod, "_admin_api_allowed"):
            from .auth_access import dashboard_api_authorized

            routes_mod._admin_api_allowed = dashboard_api_authorized
        # Keep older tests and monkey patches effective against the bytecode-only
        # auth hook, whose global name is ``dashboard_api_authorized``.
        def _p10_17_dashboard_api_authorized_proxy():
            return routes_mod._admin_api_allowed()

        routes_mod.dashboard_api_authorized = _p10_17_dashboard_api_authorized_proxy
        if not hasattr(routes_mod, "run_async_with_timeout"):
            def _p10_17_missing_run_async_with_timeout(*args, **kwargs):
                raise RuntimeError("run_async_with_timeout is unavailable in bytecode-only routes")

            routes_mod.run_async_with_timeout = _p10_17_missing_run_async_with_timeout
        return routes_mod.register_routes, routes_mod.api


def create_app() -> Flask:
    """Create and configure Flask application"""
    app = Flask(
        __name__,
        template_folder='templates',
        static_folder='static'
    )

    # Configuration
    app.config['SECRET_KEY'] = settings.dashboard.secret_key
    app.config['SQLALCHEMY_DATABASE_URI'] = settings.database.url
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    _configure_reverse_proxy_and_session(app)

    # Initialize extensions
    login_manager.init_app(app)
    login_manager.login_view = 'auth.login'
    csrf.init_app(app)

    @login_manager.unauthorized_handler
    def unauthorized():
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        from flask import redirect, url_for

        return redirect(url_for("auth.login", next=request.full_path or request.path))

    @app.before_request
    def require_story_operator_authorization():
        """Protect every current or legacy Story API before it reaches state."""
        if not request.path.startswith("/api/stories"):
            return None
        from src.dashboard.auth_access import dashboard_api_authorized

        if not dashboard_api_authorized():
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        return None

    # Ensure all DB tables exist (additive — never drops columns)
    from src.core.database import init_db
    init_db()

    try:
        from src.ai_agent.account_allowlist import RESERVED_AI_AGENT_ACCOUNT_IDS

        logger.info(
            "reserved_ai_account_ids",
            reserved_ai_account_ids=sorted(RESERVED_AI_AGENT_ACCOUNT_IDS),
        )
    except Exception as e:
        logger.warning("reserved_ai_account_ids_log_failed", error=str(e))

    # Background readiness resolver (non-blocking; one per process).
    try:
        from src.clients.readiness_worker import start_readiness_worker_background

        start_readiness_worker_background()
    except Exception as e:
        logger.warning("readiness_worker_start_failed", error=str(e))

    try:
        from src.ai_agent.account_allowlist import log_ai_agent_inbound_allowlist_at_startup

        log_ai_agent_inbound_allowlist_at_startup()
    except Exception as e:
        logger.warning("ai_agent_inbound_allowlist_startup_failed", error=str(e))

    try:
        from src.ai_agent.auto_loop_worker import start_ai_agent_auto_loop_background

        start_ai_agent_auto_loop_background()
    except Exception as e:
        logger.warning("ai_agent_auto_loop_start_failed", error=str(e))

    # Story operator routes are part of the required clean runtime boundary.
    from .auth_routes import auth
    from .story_rotation_routes import story_rotation_api

    app.register_blueprint(auth)
    app.register_blueprint(story_rotation_api)

    # Other dashboard subsystems are optional features. Their incomplete source
    # must not prevent the core web/Story safety boundary from starting.
    optional_blueprints: list[Any] = []
    optional_blueprints += _load_optional_blueprints(
        app, "src.dashboard.operational_state_routes", ("operational_state_api",)
    )
    optional_blueprints += _load_optional_blueprints(
        app, "src.dashboard.scheduler_routes", ("scheduler_api",)
    )
    optional_blueprints += _load_optional_blueprints(
        app, "src.dashboard.ai_agent_routes", ("ai_agent_api",)
    )
    optional_blueprints += _load_optional_blueprints(
        app, "src.dashboard.readiness_v2_routes", ("readiness_v2_api",)
    )
    optional_blueprints += _load_optional_blueprints(
        app, "src.dashboard.accounts_v2_routes", ("accounts_v2_bp",)
    )
    optional_blueprints += _load_optional_blueprints(
        app, "src.dashboard.governance_routes", ("governance_api",)
    )
    optional_blueprints += _load_optional_blueprints(
        app, "src.dashboard.broadcast_routes", ("broadcast_bp", "broadcast_api")
    )
    optional_blueprints += _load_optional_blueprints(
        app, "src.dashboard.ai_coding_routes", ("ai_coding_bp", "ai_coding_api")
    )
    optional_blueprints += _load_optional_blueprints(
        app, "src.dashboard.operator_control_routes", ("operator_control_bp",)
    )

    register_routes, api = _import_dashboard_routes()
    if register_routes is not None and api is not None:
        try:
            register_routes(app)
        except Exception as e:
            logger.warning(
                "dashboard_legacy_routes_register_failed",
                error=str(e),
                hint="P9.11: legacy routes.pyc may be partial; P9 API routes still load",
            )
        finally:
            # Legacy route registration can partially succeed before import-time
            # optional dependencies fail. Exempt the API blueprint regardless so
            # dashboard fetch() calls receive JSON route/auth errors, not HTML
            # Flask-WTF CSRF pages.
            csrf.exempt(api)
    _ensure_operational_state_p9_handler(app)
    _ensure_legacy_api_guards(app)
    _ensure_p3_discovery_api_guards(app)
    _ensure_p3_deep_health_route(app)
    _ensure_p10_17_accounts_api(app)
    _ensure_p10_17_story_precheck_compat(app)
    _ensure_p3_story_publish_gate(app)
    _ensure_dexpert_audit_route(app)
    try:
        from .accounts_legacy_redirect import ensure_accounts_legacy_redirect

        ensure_accounts_legacy_redirect(app)
    except Exception as exc:
        logger.warning(
            "optional_accounts_redirect_unavailable",
            error_class=type(exc).__name__,
            error=str(exc),
        )

    csrf.exempt(story_rotation_api)
    for blueprint in optional_blueprints:
        if (blueprint.url_prefix or "").startswith("/api"):
            csrf.exempt(blueprint)

    # Inject admin token into every template context so JS can send it
    @app.context_processor
    def inject_admin_token():
        inject = os.environ.get("DASHBOARD_INJECT_ADMIN_TOKEN", "false").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        return {"admin_token": os.environ.get("DASHBOARD_ADMIN_TOKEN", "") if inject else ""}

    # Error handlers
    @app.after_request
    def enforce_api_json_error_contract(response):
        if not request.path.startswith("/api/") or response.status_code < 400:
            return response
        if response.is_json:
            data = response.get_json(silent=True)
            if isinstance(data, dict) and "ok" not in data:
                data = {"ok": False, **data}
                return make_response(jsonify(data), response.status_code)
            return response
        if response.mimetype == "text/html":
            return make_response(jsonify(
                {
                    "ok": False,
                    "error": "api_error",
                    "status": response.status_code,
                    "message": "API returned an HTML error page",
                }
            ), response.status_code)
        return response

    @app.errorhandler(CSRFError)
    def csrf_error(error):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "csrf_failed", "message": str(error.description)}), 400
        return {"error": "CSRF failed"}, 400

    @app.errorhandler(405)
    def method_not_allowed(error):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "method_not_allowed"}), 405
        return {"error": "Method not allowed"}, 405

    @app.errorhandler(404)
    def not_found(error):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "not_found"}), 404
        return {"error": "Not found"}, 404

    @app.errorhandler(500)
    def internal_error(error):
        tb = traceback.format_exc()
        logger.error("Internal server error", error=str(error), traceback=tb)
        # Also print to stderr so journalctl captures it
        import sys
        print(traceback.format_exc(), file=sys.stderr, flush=True)
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "internal_server_error"}), 500
        return {"error": "Internal server error"}, 500

    # Operational sentinel: makes drift between source and running process
    # visible in journalctl. If a deploy edits scheduler_routes.py without
    # restarting the worker, the next boot's log will tell us immediately.
    _scheduler_route_count = sum(
        1 for r in app.url_map.iter_rules() if r.rule.startswith("/api/v1/")
    )
    _readiness_present = any(
        r.rule == "/api/v1/accounts/readiness" for r in app.url_map.iter_rules()
    )
    logger.info(
        "Flask app created",
        environment=settings.environment,
        scheduler_api_routes=_scheduler_route_count,
        readiness_route="present" if _readiness_present else "MISSING",
    )
    return app


@login_manager.user_loader
def load_user(user_id):
    """Load user for Flask-Login (uses raw SQLAlchemy, not Flask-SQLAlchemy)"""
    try:
        from .models import DashboardUser
        from src.core.database import get_db_context
        with get_db_context() as db:
            return db.get(DashboardUser, int(user_id))
    except Exception:
        return None
