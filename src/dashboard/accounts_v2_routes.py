"""
P9.9 / P9.18 — Accounts v2 dashboard page (feature-flagged, read-only).

GET /accounts-v2 returns 404 when ``ACCOUNTS_V2_DASHBOARD_ENABLED`` is false.
No POST handlers; actions remain disabled in template.

P9.18: without ``?ids=``, lists full discovered fleet (DB + sessions + readiness).
"""
from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request, url_for

from config.settings import settings
from src.core.database import get_db_context
from src.dashboard.accounts_v2_fleet import (
    normalize_pagination,
    normalize_status_filter,
    normalize_tier_filter,
    parse_ids_param,
)
from src.dashboard.accounts_v2_view import build_accounts_v2_listing, project_root
from src.dashboard.auth_access import dashboard_api_authorized

accounts_v2_bp = Blueprint("accounts_v2", __name__)


@accounts_v2_bp.before_request
def accounts_v2_require_dashboard_auth():
    if not dashboard_api_authorized():
        return jsonify({"error": "unauthorized"}), 401


@accounts_v2_bp.route("/accounts-v2", methods=["GET"])
def accounts_v2_page():
    """
    Read-only accounts v2 shell.

    - ``?ids=`` — explicit pilot/subset
    - no ``?ids=`` — full fleet (discovered)
    - ``?tier=``, ``?status=``, ``?limit=``, ``?offset=``

    Disabled when ``ACCOUNTS_V2_DASHBOARD_ENABLED`` is false (404).
    """
    if not settings.accounts_v2_dashboard_enabled:
        return jsonify({"error": "not_found"}), 404

    ids_raw = (request.args.get("ids") or "").strip()
    account_ids: list[int] | None
    if ids_raw:
        try:
            account_ids = parse_ids_param(ids_raw)
        except ValueError:
            return jsonify({"error": "invalid_ids"}), 400
        if not account_ids:
            return jsonify({"error": "invalid_ids"}), 400
    else:
        account_ids = None

    try:
        tier_filter = normalize_tier_filter(request.args.get("tier"))
        status_filter = normalize_status_filter(request.args.get("status"))
        offset, limit = normalize_pagination(
            request.args.get("offset", type=int),
            request.args.get("limit", type=int),
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    with get_db_context() as db:
        ctx = build_accounts_v2_listing(
            db,
            account_ids=account_ids,
            tier_filter=tier_filter,
            status_filter=status_filter,
            offset=offset,
            limit=limit,
            root=project_root(),
            css_href=url_for("static", filename="css/accounts_v2.css"),
            live_route=True,
        )

    return render_template("accounts_v2.html", **ctx)
