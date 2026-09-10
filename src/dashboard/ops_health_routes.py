"""Wave L — authenticated ops health API (no Telegram / OpenAI probes)."""
from __future__ import annotations

from datetime import datetime, timezone

from flask import Blueprint, jsonify

from src.dashboard.auth_access import dashboard_api_authorized
from src.ops.ops_health import build_ops_health_report, load_previous_state

ops_health_api = Blueprint("ops_health_api", __name__, url_prefix="/api")


@ops_health_api.before_request
def _auth():
    if not dashboard_api_authorized():
        return jsonify({"error": "unauthorized"}), 401
    return None


def _cached_is_fresh(previous: dict, *, max_age_sec: float = 600.0) -> bool:
    text = str(previous.get("generated_at") or "").strip()
    if not text or not previous.get("checks"):
        return False
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        generated = datetime.fromisoformat(text)
    except ValueError:
        return False
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - generated.astimezone(timezone.utc)).total_seconds()
    return 0 <= age < max_age_sec


@ops_health_api.route("/ops-health", methods=["GET"])
def ops_health():
    """Consolidated Storyfleet ops health. Cheap; observes existing evidence only."""
    previous = load_previous_state()
    if _cached_is_fresh(previous):
        body = dict(previous)
        body["source"] = "cached"
        return jsonify(body)

    report = build_ops_health_report(previous_state=previous, run_quick_check=False)
    report["source"] = "live"
    return jsonify(report)
