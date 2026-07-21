"""
P9.1 — Read-only fleet operational state API (feature-flagged).
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from config.settings import settings
from src.core.account_operational_state import compute_operational_states
from src.core.database import get_db_context
from src.dashboard.auth_access import dashboard_api_authorized

operational_state_api = Blueprint("operational_state_api", __name__, url_prefix="/api")


@operational_state_api.before_request
def operational_state_api_require_dashboard_auth():
    if not dashboard_api_authorized():
        return jsonify({"error": "unauthorized"}), 401


@operational_state_api.route("/accounts/operational-state", methods=["GET"])
def accounts_operational_state():
    """
    Read-only operational truth per account (no Telethon connect, no DB writes).

    Disabled when ``FLEET_OPERATIONAL_STATE_API_ENABLED`` is false (404).
    """
    if not settings.fleet_operational_state_api_enabled:
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
        rows = compute_operational_states(db, account_ids=account_ids)

    return jsonify(
        {
            "enabled": True,
            "count": len(rows),
            "accounts": rows,
        }
    )
