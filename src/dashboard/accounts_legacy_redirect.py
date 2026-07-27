"""P10.13 — Restore normal GET /accounts while keeping /accounts-v2 diagnostics."""
from __future__ import annotations

from flask import Flask, current_app, redirect, render_template, request, url_for
import structlog

from src.core.database import get_db_context
from src.dashboard.auth_access import dashboard_api_authorized
from src.dashboard.accounts_main_view import (
    DEFAULT_GOVERNANCE_ROLES_AVAILABLE,
    build_accounts_main_context,
    empty_eligibility_preview,
)
from src.stories.story_readiness_resolver import empty_readiness_preview as empty_story_readiness_preview

logger = structlog.get_logger(__name__)


def _ensure_governance_template_context(ctx: dict) -> dict:
    """Guarantee accounts_main.html keys exist (legacy-safe)."""
    ctx.setdefault("eligibility_preview", empty_eligibility_preview("stories"))
    ctx.setdefault("eligibility_scheduler", empty_eligibility_preview("scheduler"))
    ctx.setdefault("eligibility_discovery", empty_eligibility_preview("discovery"))
    ctx.setdefault("governance_roles_available", list(DEFAULT_GOVERNANCE_ROLES_AVAILABLE))
    ctx.setdefault("story_readiness_preview", empty_story_readiness_preview())
    ctx.setdefault("pinned_account_ids", [])
    ctx.setdefault(
        "operator_summary",
        {
            "total_accounts": ctx.get("total_accounts") or 0,
            "authorized": 0,
            "certified": 0,
            "ready": 0,
            "needs_session": 0,
            "needs_attention": 0,
            "disabled": 0,
            "protected": 0,
            "reserved": 0,
            "unavailable": 0,
            "blocked": 0,
            "check_required": 0,
            "default_filter": "all",
        },
    )
    ctx.setdefault(
        "freshness_compact",
        {
            "last_verified": None,
            "valid_for_hours": 24,
            "source_label": "Missing",
            "fresh": False,
            "label": "MISSING",
        },
    )
    ctx.setdefault("default_filter", "all")
    ctx.setdefault("automation_locked", True)
    ctx.setdefault("canonical_source", "/opt/autostory/data/fleet-readiness/latest.json")
    for account in ctx.get("accounts") or []:
        if not isinstance(account, dict):
            continue
        account.setdefault("governance_badges", [])
        account.setdefault("governance_blocked_reasons", [])
        account.setdefault("requires_manual_override", False)
        account.setdefault("pinned", False)
        account.setdefault("display_status", "CHECK_REQUIRED")
        account.setdefault("status_label", "Check required")
        account.setdefault("status_detail", "Run authorization probe")
        account.setdefault("authorization_label", "Unknown")
        account.setdefault("role_label", "Unknown")
        account.setdefault("required_action", "None")
        account.setdefault("severity", "warning")
        account.setdefault("filter_group", "all")
        account.setdefault("canonical", {})
        account.setdefault("modules", {"campaigns": {"label": "—"}})
    return ctx

LEGACY_ACCOUNTS_ENDPOINT = "web.accounts_page"


def accounts_main_page():
    """Render the normal operator Accounts page; no account mutations."""
    if not dashboard_api_authorized():
        return redirect(url_for("auth.login", next=request.full_path or request.path))
    try:
        with get_db_context() as db:
            ctx = build_accounts_main_context(db)
    except Exception as exc:
        logger.exception("accounts_main_context_failed", error=str(exc))
        ctx = {
            "accounts": [],
            "generated_at": "",
            "total_accounts": 0,
            "operational_count": 0,
            "scheduler_locked": True,
            "campaigns_locked": True,
        }
    ctx = _ensure_governance_template_context(ctx)
    endpoints = current_app.view_functions
    ctx["account_action_routes"] = {
        "add_account": "api.start_auth" in endpoints,
        "upload_tdata": "api.import_tdata" in endpoints,
        "import_session": "api.import_session" in endpoints,
        "diagnostics": "accounts_v2.accounts_v2_page" in endpoints,
    }
    return render_template("accounts_main.html", **ctx)


def ensure_accounts_legacy_redirect(app: Flask) -> None:
    """
    Replace legacy ``accounts_page`` handler with the normal operator page.

    The function name is retained for compatibility with app.py call sites.
    """
    if LEGACY_ACCOUNTS_ENDPOINT not in app.view_functions:
        if not any(rule.rule == "/accounts" for rule in app.url_map.iter_rules()):
            app.add_url_rule("/accounts", endpoint=LEGACY_ACCOUNTS_ENDPOINT, view_func=accounts_main_page)
            logger.info("accounts_main_route_registered", endpoint=LEGACY_ACCOUNTS_ENDPOINT)
        else:
            logger.warning(
                "accounts_main_route_skipped",
                reason="endpoint_missing_but_rule_present",
                expected=LEGACY_ACCOUNTS_ENDPOINT,
            )
        return

    patched = accounts_main_page
    patched.__name__ = "accounts_page"
    app.view_functions[LEGACY_ACCOUNTS_ENDPOINT] = patched
    logger.info(
        "accounts_main_route_restored",
        legacy_endpoint=LEGACY_ACCOUNTS_ENDPOINT,
        diagnostics="/accounts-v2",
    )
