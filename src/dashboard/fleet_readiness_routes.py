"""Read-only fleet Story readiness matrix for operators.

Loads the latest published matrix from shared data. No Telegram probes,
no publish controls, no secret fields.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, render_template
from flask_login import login_required

from src.stories.fleet_readiness_matrix import load_latest_matrix

fleet_readiness_bp = Blueprint("fleet_readiness", __name__)


@fleet_readiness_bp.route("/stories/fleet-readiness")
@login_required
def fleet_readiness_page():
    matrix = load_latest_matrix()
    return render_template(
        "fleet_readiness.html",
        matrix=matrix,
        totals=(matrix or {}).get("totals") or {},
        accounts=(matrix or {}).get("accounts") or [],
    )


@fleet_readiness_bp.route("/api/stories/fleet-readiness")
@login_required
def fleet_readiness_api():
    matrix = load_latest_matrix()
    if matrix is None:
        return jsonify({"ok": False, "error": "No fleet readiness matrix published yet"}), 404
    # Defense-in-depth redaction
    safe_accounts = []
    for row in matrix.get("accounts") or []:
        safe_accounts.append(
            {
                k: v
                for k, v in row.items()
                if k
                not in {
                    "session_string",
                    "api_hash",
                    "password",
                    "phone_number",
                    "auth_key",
                }
            }
        )
    return jsonify(
        {
            "ok": True,
            "generated_at": matrix.get("generated_at"),
            "source_audit_run_id": matrix.get("source_audit_run_id"),
            "totals": matrix.get("totals"),
            "accounts": safe_accounts,
            "publish_controls": False,
            "secrets_redacted": True,
        }
    )
