"""Read-only fleet Story readiness matrix for operators.

Uses the shared operator presentation mapper and the canonical
``/opt/autostory/data/fleet-readiness/latest.json`` artifact.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, render_template
from flask_login import login_required

from src.stories.fleet_readiness_matrix import load_latest_matrix, matrix_freshness
from src.stories.operator_account_presentation import build_operator_account_views

fleet_readiness_bp = Blueprint("fleet_readiness", __name__)


@fleet_readiness_bp.route("/stories/fleet-readiness")
@login_required
def fleet_readiness_page():
    matrix = load_latest_matrix()
    freshness = matrix_freshness(matrix)
    views = build_operator_account_views(matrix, freshness=freshness)
    if matrix is not None:
        matrix = {**matrix, "freshness": {**(matrix.get("freshness") or {}), **freshness}}
    return render_template(
        "fleet_readiness.html",
        matrix=matrix,
        totals=(matrix or {}).get("totals") or {},
        accounts=(matrix or {}).get("accounts") or [],
        operator_accounts=views["accounts"],
        operator_summary=views["summary"],
        freshness=freshness,
        freshness_compact=views["summary"]["freshness_compact"],
    )


@fleet_readiness_bp.route("/api/stories/fleet-readiness")
@login_required
def fleet_readiness_api():
    matrix = load_latest_matrix()
    if matrix is None:
        return jsonify({"ok": False, "error": "No fleet readiness matrix published yet"}), 404
    freshness = matrix_freshness(matrix)
    views = build_operator_account_views(matrix, freshness=freshness)
    safe_accounts = []
    for row in views["accounts"]:
        safe_accounts.append(
            {
                "account_id": row["account_id"],
                "account_label": row["account_label"],
                "display_status": row["display_status"],
                "status_label": row["status_label"],
                "status_detail": row["status_detail"],
                "authorization_label": row["authorization_label"],
                "role_label": row["role_label"],
                "required_action": row["required_action"],
                "severity": row["severity"],
                "last_checked": row["last_checked"],
                "canonical_classification": (row.get("canonical") or {}).get("classification"),
            }
        )
    return jsonify(
        {
            "ok": True,
            "generated_at": matrix.get("generated_at"),
            "source_audit_run_id": matrix.get("source_audit_run_id"),
            "source_audit_completed_at": matrix.get("source_audit_completed_at"),
            "freshness": freshness,
            "freshness_compact": views["summary"]["freshness_compact"],
            "operator_summary": views["summary"],
            "accounts": safe_accounts,
            "publish_controls": False,
            "secrets_redacted": True,
            "canonical_source": "/opt/autostory/data/fleet-readiness/latest.json",
        }
    )
