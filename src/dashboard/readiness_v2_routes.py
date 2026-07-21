"""
P9.7 — Read-only readiness v2 snapshots API (feature-flagged).
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from config.settings import settings
from src.core.database import get_db_context
from src.dashboard.auth_access import dashboard_api_authorized
from src.readiness.readiness_v2_observer import build_v2_snapshots

readiness_v2_api = Blueprint("readiness_v2_api", __name__, url_prefix="/api")


@readiness_v2_api.before_request
def readiness_v2_api_require_dashboard_auth():
    if not dashboard_api_authorized():
        return jsonify({"error": "unauthorized"}), 401


@readiness_v2_api.route("/readiness/v2/snapshots", methods=["GET"])
def readiness_v2_snapshots():
    """
    Read-only v2 readiness rows (not authoritative; v1 unchanged).

    Disabled when ``READINESS_V2_API_ENABLED`` is false (404).
    """
    if not settings.readiness_v2_api_enabled:
        return jsonify({"error": "not_found"}), 404

    account_id = request.args.get("account_id", type=int)
    ids_raw = (request.args.get("ids") or "").strip()
    account_ids: list[int] | None = None
    if account_id is not None:
        account_ids = [int(account_id)]
    elif ids_raw:
        try:
            account_ids = [int(x.strip()) for x in ids_raw.split(",") if x.strip()]
        except ValueError:
            return jsonify({"error": "invalid_ids"}), 400

    with get_db_context() as db:
        rows = build_v2_snapshots(db, account_ids=account_ids)

    return jsonify(
        {
            "enabled": True,
            "authoritative": False,
            "count": len(rows),
            "snapshots": rows,
        }
    )
