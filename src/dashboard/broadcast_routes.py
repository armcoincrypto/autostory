"""Safe broadcast campaign page + server-side proxy (Phase 8C.1)."""
from __future__ import annotations

import structlog
from flask import Blueprint, jsonify, redirect, render_template, request, url_for
from flask_login import current_user

from .ai_coding_client import fetch_upstream
from .auth_access import dashboard_api_authorized

logger = structlog.get_logger(__name__)

broadcast_bp = Blueprint("broadcast", __name__)
broadcast_api = Blueprint("broadcast_api", __name__, url_prefix="/api/v1/broadcast")


def _actor_label() -> str:
    if current_user.is_authenticated:
        return str(getattr(current_user, "email", None) or getattr(current_user, "id", "dashboard_user"))
    return "admin_token"


def _audit(action: str, **fields) -> None:
    logger.info("broadcast_audit", action=action, actor=_actor_label(), **fields)


def _proxy(method: str, upstream_path: str, *, action: str, json_body=None, query=None):
    import json as json_lib
    import urllib.error
    import urllib.request

    from .ai_coding_client import ai_coding_api_base_url

    base = ai_coding_api_base_url()
    url = f"{base}{upstream_path}"
    if query:
        from urllib.parse import urlencode

        params = urlencode({k: v for k, v in query.items() if v})
        if params:
            url = f"{url}?{params}"

    data = None
    headers = {"Accept": "application/json"}
    if json_body is not None:
        data = json_lib.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            body = response.read().decode("utf-8")
            status = response.getcode() or 200
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        status = exc.code
    except urllib.error.URLError as exc:
        _audit(action, upstream_status=502, error=str(exc.reason))
        return jsonify({"error": "broadcast_upstream_unavailable", "detail": str(exc.reason)}), 502

    _audit(action, upstream_status=status)
    if not body.strip():
        return jsonify({}), status
    try:
        return jsonify(json_lib.loads(body)), status
    except json_lib.JSONDecodeError:
        return jsonify({"error": "broadcast_upstream_invalid_json"}), 502


@broadcast_bp.before_request
def _page_auth():
    if request.endpoint == "broadcast.broadcast_page":
        if not dashboard_api_authorized():
            return redirect(url_for("auth.login", next=request.full_path or request.path))
    return None


@broadcast_api.before_request
def _api_auth():
    if not dashboard_api_authorized():
        return jsonify({"error": "unauthorized"}), 401
    return None


@broadcast_bp.route("/broadcast", methods=["GET"])
def broadcast_page():
    _audit("broadcast_dashboard_viewed")
    return render_template("broadcast.html", api_base="/api/v1/broadcast")


@broadcast_api.route("/campaigns", methods=["GET", "POST"])
def campaigns_collection():
    if request.method == "GET":
        return _proxy("GET", "/api/v1/campaigns", action="broadcast_campaigns_listed")
    body = request.get_json(silent=True) or {}
    body.setdefault("created_by", _actor_label())
    return _proxy("POST", "/api/v1/campaigns", action="broadcast_campaign_created", json_body=body)


@broadcast_api.route("/campaigns/<campaign_id>", methods=["GET", "PATCH"])
def campaign_detail(campaign_id: str):
    if request.method == "GET":
        return _proxy("GET", f"/api/v1/campaigns/{campaign_id}", action="broadcast_campaign_viewed")
    body = request.get_json(silent=True) or {}
    return _proxy("PATCH", f"/api/v1/campaigns/{campaign_id}", action="broadcast_campaign_updated", json_body=body)


@broadcast_api.route("/campaigns/<campaign_id>/audience/import", methods=["POST"])
def campaign_audience_import(campaign_id: str):
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy(
        "POST",
        f"/api/v1/campaigns/{campaign_id}/audience/import",
        action="broadcast_audience_imported",
        json_body=body,
    )


@broadcast_api.route("/campaigns/<campaign_id>/audience/preview", methods=["GET"])
def campaign_audience_preview(campaign_id: str):
    return _proxy(
        "GET",
        f"/api/v1/campaigns/{campaign_id}/audience/preview",
        action="broadcast_audience_previewed",
    )


@broadcast_api.route("/campaigns/<campaign_id>/preview", methods=["POST"])
def campaign_message_preview(campaign_id: str):
    return _proxy("POST", f"/api/v1/campaigns/{campaign_id}/preview", action="broadcast_message_previewed")


@broadcast_api.route("/campaigns/<campaign_id>/dry-run", methods=["POST"])
def campaign_dry_run(campaign_id: str):
    return _proxy("POST", f"/api/v1/campaigns/{campaign_id}/dry-run", action="broadcast_dry_run_completed")


@broadcast_api.route("/suppression", methods=["GET", "POST"])
def suppression_collection():
    if request.method == "GET":
        return _proxy(
            "GET",
            "/api/v1/campaigns/suppression",
            action="broadcast_suppression_listed",
            query={
                "channel": request.args.get("channel"),
                "search": request.args.get("search"),
            },
        )
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy("POST", "/api/v1/campaigns/suppression", action="broadcast_suppression_added", json_body=body)


@broadcast_api.route("/suppression/<entry_id>", methods=["DELETE"])
def suppression_remove(entry_id: str):
    return _proxy(
        "DELETE",
        f"/api/v1/campaigns/suppression/{entry_id}",
        action="broadcast_suppression_removed",
        query={"actor": _actor_label()},
    )


def _proxy_raw(method: str, upstream_path: str, *, action: str, query=None):
    """Proxy non-JSON responses (exports)."""
    import urllib.error
    import urllib.request

    from .ai_coding_client import ai_coding_api_base_url

    base = ai_coding_api_base_url()
    url = f"{base}{upstream_path}"
    if query:
        from urllib.parse import urlencode

        params = urlencode({k: v for k, v in query.items() if v is not None})
        if params:
            url = f"{url}?{params}"

    req = urllib.request.Request(url, headers={"Accept": "*/*"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            body = response.read()
            status = response.getcode() or 200
            headers = dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        body = exc.read()
        status = exc.code
        headers = dict(exc.headers.items()) if exc.headers else {}
    except urllib.error.URLError as exc:
        _audit(action, upstream_status=502, error=str(exc.reason))
        return jsonify({"error": "broadcast_upstream_unavailable", "detail": str(exc.reason)}), 502

    _audit(action, upstream_status=status)
    from flask import Response as FlaskResponse

    resp = FlaskResponse(body, status=status)
    for key in ("Content-Type", "Content-Disposition"):
        if key in headers:
            resp.headers[key] = headers[key]
    return resp


@broadcast_api.route("/campaigns/<campaign_id>/validation", methods=["GET"])
def campaign_validation(campaign_id: str):
    return _proxy("GET", f"/api/v1/campaigns/{campaign_id}/validation", action="broadcast_validation_viewed")


@broadcast_api.route("/campaigns/<campaign_id>/audit", methods=["GET"])
def campaign_audit(campaign_id: str):
    return _proxy("GET", f"/api/v1/campaigns/{campaign_id}/audit", action="broadcast_audit_viewed")


@broadcast_api.route("/campaigns/<campaign_id>/submit-review", methods=["POST"])
def campaign_submit_review(campaign_id: str):
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy(
        "POST",
        f"/api/v1/campaigns/{campaign_id}/submit-review",
        action="broadcast_review_submitted",
        json_body=body,
    )


@broadcast_api.route("/campaigns/<campaign_id>/approve", methods=["POST"])
def campaign_approve(campaign_id: str):
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy(
        "POST",
        f"/api/v1/campaigns/{campaign_id}/approve",
        action="broadcast_campaign_approved",
        json_body=body,
    )


@broadcast_api.route("/campaigns/<campaign_id>/reject", methods=["POST"])
def campaign_reject(campaign_id: str):
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy(
        "POST",
        f"/api/v1/campaigns/{campaign_id}/reject",
        action="broadcast_campaign_rejected",
        json_body=body,
    )


@broadcast_api.route("/campaigns/<campaign_id>/cancel", methods=["POST"])
def campaign_cancel(campaign_id: str):
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy(
        "POST",
        f"/api/v1/campaigns/{campaign_id}/cancel",
        action="broadcast_campaign_cancelled",
        json_body=body,
    )


@broadcast_api.route("/campaigns/<campaign_id>/dry-run/export", methods=["GET"])
def campaign_dry_run_export(campaign_id: str):
    return _proxy_raw(
        "GET",
        f"/api/v1/campaigns/{campaign_id}/dry-run/export",
        action="broadcast_dry_run_exported",
        query={
            "fmt": request.args.get("fmt", "markdown"),
            "actor": _actor_label(),
        },
    )


@broadcast_api.route("/campaigns/<campaign_id>/send/preview", methods=["GET"])
def campaign_send_preview(campaign_id: str):
    return _proxy(
        "GET",
        f"/api/v1/campaigns/{campaign_id}/send/preview",
        action="broadcast_send_preview_viewed",
        query={
            "limit": request.args.get("limit", "5"),
            "actor": _actor_label(),
        },
    )


@broadcast_api.route("/campaigns/<campaign_id>/send/tiny-cohort", methods=["POST"])
def campaign_send_tiny_cohort(campaign_id: str):
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy(
        "POST",
        f"/api/v1/campaigns/{campaign_id}/send/tiny-cohort",
        action="broadcast_tiny_cohort_send",
        json_body=body,
    )


@broadcast_api.route("/campaigns/<campaign_id>/send/batches", methods=["GET"])
def campaign_send_batches(campaign_id: str):
    return _proxy(
        "GET",
        f"/api/v1/campaigns/{campaign_id}/send/batches",
        action="broadcast_send_batches_listed",
    )


@broadcast_api.route("/campaigns/<campaign_id>/send/batches/<batch_id>", methods=["GET"])
def campaign_send_batch_detail(campaign_id: str, batch_id: str):
    return _proxy(
        "GET",
        f"/api/v1/campaigns/{campaign_id}/send/batches/{batch_id}",
        action="broadcast_send_batch_viewed",
    )
