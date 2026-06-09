"""AI Coding review transparency page + server-side API proxy."""
from __future__ import annotations

import json
import structlog
from flask import Blueprint, Response, jsonify, redirect, render_template, request, url_for
from flask_login import current_user

from .ai_coding_client import (
    ai_coding_api_base_url,
    fetch_upstream,
    fetch_upstream_health,
    fetch_upstream_method,
    fetch_upstream_raw,
)
from .auth_access import dashboard_api_authorized

logger = structlog.get_logger(__name__)

# Bust browser cache when operator Simple Mode assets change.
AI_CODING_STATIC_VERSION = "operator-trust-3"

ai_coding_bp = Blueprint("ai_coding", __name__)
ai_coding_api = Blueprint("ai_coding_api", __name__, url_prefix="/api/v1/ai-coding")


def _require_auth():
    if not dashboard_api_authorized():
        return jsonify({"error": "unauthorized"}), 401
    return None


def _actor_label() -> str:
    if current_user.is_authenticated:
        return str(getattr(current_user, "email", None) or getattr(current_user, "id", "dashboard_user"))
    return "admin_token"


def _audit(action: str, **fields) -> None:
    logger.info("ai_coding_audit", action=action, actor=_actor_label(), **fields)


def _proxy_get(upstream_path: str, *, action: str, query: dict[str, str] | None = None, **audit_fields):
    status, payload = fetch_upstream(upstream_path, query=query)
    _audit(action, upstream_status=status, **audit_fields)
    return jsonify(payload), status


@ai_coding_bp.before_request
def _page_auth():
    if request.endpoint in {
        "ai_coding.ai_coding_page",
        "ai_coding.ai_coding_intelligence_page",
        "ai_coding.ai_coding_planner_page",
        "ai_coding.ai_coding_executions_page",
        "ai_coding.ai_coding_deployments_page",
        "ai_coding.ai_coding_audit_coverage_page",
        "ai_coding.ai_coding_factory_analytics_page",
        "ai_coding.ai_coding_builds_page",
    }:
        if not dashboard_api_authorized():
            return redirect(url_for("auth.login", next=request.full_path or request.path))
    return None


@ai_coding_api.before_request
def _api_auth():
    return _require_auth()


@ai_coding_bp.route("/ai-coding", methods=["GET"])
def ai_coding_page():
    _audit("ai_coding_dashboard_viewed")
    return render_template(
        "ai_coding.html",
        api_base="/api/v1/ai-coding",
        upstream_configured=bool(ai_coding_api_base_url()),
        ai_coding_static_version=AI_CODING_STATIC_VERSION,
    )


@ai_coding_api.route("/health", methods=["GET"])
def ai_coding_health():
    status, payload = fetch_upstream_health()
    _audit("ai_coding_health_checked", upstream_status=status, connected=payload.get("connected"))
    return jsonify(payload), status


@ai_coding_api.route("/audit", methods=["POST"])
def ai_coding_audit_event():
    data = request.get_json(silent=True) or {}
    action = str(data.get("action") or "ai_coding_client_event").strip()
    execution_id = data.get("execution_id")
    _audit(action, execution_id=execution_id, client_event=True)
    return jsonify({"ok": True}), 200


@ai_coding_api.route("/reviews/recent", methods=["GET"])
def reviews_recent():
    query: dict[str, str] = {
        "limit": request.args.get("limit", "50"),
        "offset": request.args.get("offset", "0"),
    }
    for key in ("verdict", "project_id", "phase_id", "source"):
        value = (request.args.get(key) or "").strip()
        if value:
            query[key] = value
    if request.args.get("has_blockers") is not None:
        query["has_blockers"] = request.args.get("has_blockers", "")
    if request.args.get("include_archived") is not None:
        query["include_archived"] = request.args.get("include_archived", "")
    return _proxy_get("/api/v1/reviews/recent", action="ai_coding_reviews_listed", query=query)


@ai_coding_api.route("/reviews/<execution_id>", methods=["GET"])
def review_detail(execution_id: str):
    return _proxy_get(
        f"/api/v1/reviews/{execution_id}",
        action="ai_coding_review_opened",
        execution_id=execution_id,
    )


@ai_coding_api.route("/reviews/<execution_id>/risk-summary", methods=["GET"])
def review_risk_summary(execution_id: str):
    return _proxy_get(
        f"/api/v1/reviews/{execution_id}/risk-summary",
        action="ai_coding_risk_summary_viewed",
        execution_id=execution_id,
    )


@ai_coding_api.route("/executions/recent", methods=["GET"])
def executions_recent():
    query: dict[str, str] = {
        "limit": request.args.get("limit", "50"),
        "offset": request.args.get("offset", "0"),
    }
    for key in ("status", "project_id"):
        value = (request.args.get(key) or "").strip()
        if value:
            query[key] = value
    return _proxy_get("/api/v1/action-console/executions/recent", action="ai_coding_executions_listed", query=query)


@ai_coding_api.route("/executions/<execution_id>", methods=["GET"])
def execution_detail(execution_id: str):
    return _proxy_get(
        f"/api/v1/action-console/executions/{execution_id}",
        action="ai_coding_execution_viewed",
        execution_id=execution_id,
    )


@ai_coding_api.route("/executions/<execution_id>/logs", methods=["GET"])
def execution_logs(execution_id: str):
    query = {
        "offset": request.args.get("offset", "0"),
        "limit": request.args.get("limit", "200"),
    }
    return _proxy_get(
        f"/api/v1/action-console/executions/{execution_id}/logs",
        action="ai_coding_logs_viewed",
        execution_id=execution_id,
        query=query,
    )


@ai_coding_api.route("/executions/<execution_id>/timeline", methods=["GET"])
def execution_timeline(execution_id: str):
    return _proxy_get(
        f"/api/v1/action-console/executions/{execution_id}/timeline",
        action="ai_coding_timeline_viewed",
        execution_id=execution_id,
    )


@ai_coding_api.route("/executions/<execution_id>/operator-review", methods=["POST"])
def execution_operator_review(execution_id: str):
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy_write(
        "POST",
        f"/api/v1/executions/{execution_id}/operator-review",
        action="execution_operator_review",
        json_body=body,
        execution_id=execution_id,
    )


@ai_coding_api.route("/executions/<execution_id>/recommended-fixes", methods=["GET"])
def execution_recommended_fixes(execution_id: str):
    return _proxy_get(
        f"/api/v1/action-console/executions/{execution_id}/recommended-fixes",
        action="ai_coding_recommended_fixes_viewed",
        execution_id=execution_id,
    )


@ai_coding_api.route("/projects/platform", methods=["GET"])
def projects_platform_list():
    return _proxy_get("/api/v1/projects/platform", action="projects_platform_list")


@ai_coding_api.route("/projects/<project_id>/platform", methods=["GET"])
def project_platform_detail(project_id: str):
    return _proxy_get(
        f"/api/v1/projects/{project_id}/platform",
        action="project_platform_detail",
    )


@ai_coding_api.route("/projects/<project_id>/health", methods=["GET"])
def project_health(project_id: str):
    return _proxy_get(
        f"/api/v1/projects/{project_id}/health",
        action="project_health",
    )


@ai_coding_api.route("/projects/<project_id>/jobs", methods=["GET"])
def project_jobs(project_id: str):
    return _proxy_get(
        f"/api/v1/projects/{project_id}/jobs",
        action="project_jobs",
    )


@ai_coding_api.route("/projects/<project_id>/release-policy", methods=["GET"])
def project_release_policy(project_id: str):
    return _proxy_get(
        f"/api/v1/projects/{project_id}/release-policy",
        action="project_release_policy",
    )


@ai_coding_api.route("/projects/summary", methods=["GET"])
def projects_summary():
    query = {"limit": request.args.get("limit", "50")}
    return _proxy_get("/api/v1/action-console/projects/summary", action="ai_coding_projects_summary", query=query)


@ai_coding_api.route("/builds/recent", methods=["GET"])
def builds_recent():
    query: dict[str, str] = {
        "limit": request.args.get("limit", "25"),
        "offset": request.args.get("offset", "0"),
    }
    project_id = (request.args.get("project_id") or "").strip()
    if project_id:
        query["project_id"] = project_id
    return _proxy_get("/api/v1/action-console/builds/recent", action="ai_coding_builds_listed", query=query)


def _proxy_write(method: str, upstream_path: str, *, action: str, json_body=None, query=None, **audit_fields):
    status, payload = fetch_upstream_method(method, upstream_path, query=query, json_body=json_body)
    _audit(action, upstream_status=status, **audit_fields)
    return jsonify(payload), status


@ai_coding_bp.route("/ai-coding/projects/<project_id>/intelligence", methods=["GET"])
def ai_coding_intelligence_page(project_id: str):
    _audit("ai_coding_intelligence_page_viewed", project_id=project_id)
    return render_template(
        "ai_coding_intelligence.html",
        project_id=project_id,
        api_base="/api/v1/ai-coding",
    )


@ai_coding_bp.route("/ai-coding/planner", methods=["GET"])
@ai_coding_bp.route("/ai-coding/planner/<plan_id>", methods=["GET"])
def ai_coding_planner_page(plan_id: str | None = None):
    _audit("ai_coding_planner_page_viewed", plan_id=plan_id or "")
    return render_template(
        "ai_coding_planner.html",
        plan_id=plan_id,
        api_base="/api/v1/ai-coding",
    )


@ai_coding_bp.route("/ai-coding/executions", methods=["GET"])
@ai_coding_bp.route("/ai-coding/executions/<program_id>", methods=["GET"])
def ai_coding_executions_page(program_id: str | None = None):
    _audit("ai_coding_executions_page_viewed", program_id=program_id or "")
    return render_template(
        "ai_coding_executions.html",
        program_id=program_id,
        api_base="/api/v1/ai-coding",
    )


@ai_coding_bp.route("/ai-coding/deployments", methods=["GET"])
@ai_coding_bp.route("/ai-coding/deployments/<plan_id>", methods=["GET"])
def ai_coding_deployments_page(plan_id: str | None = None):
    _audit("ai_coding_deployments_page_viewed", plan_id=plan_id or "")
    return render_template(
        "ai_coding_deployments.html",
        plan_id=plan_id,
        api_base="/api/v1/ai-coding",
    )


@ai_coding_bp.route("/ai-coding/builds", methods=["GET"])
@ai_coding_bp.route("/ai-coding/builds/<run_id>", methods=["GET"])
def ai_coding_builds_page(run_id: str | None = None):
    _audit("ai_coding_builds_page_viewed", run_id=run_id or "")
    return render_template(
        "ai_coding_builds.html",
        run_id=run_id,
        api_base="/api/v1/ai-coding",
        ai_coding_static_version=AI_CODING_STATIC_VERSION,
    )


@ai_coding_bp.route("/ai-coding/audit-coverage", methods=["GET"])
def ai_coding_audit_coverage_page():
    _audit("ai_coding_audit_coverage_viewed")
    return render_template(
        "ai_coding_audit_coverage.html",
        api_base="/api/v1/ai-coding",
    )


@ai_coding_bp.route("/ai-coding/factory-analytics", methods=["GET"])
def ai_coding_factory_analytics_page():
    _audit("ai_coding_factory_analytics_viewed")
    return render_template(
        "ai_coding_factory_analytics.html",
        api_base="/api/v1/ai-coding",
    )


@ai_coding_api.route("/deployment-governance/plans", methods=["GET", "POST"])
def deployment_governance_plans():
    if request.method == "GET":
        query = {
            "limit": request.args.get("limit", "50"),
            "status": request.args.get("status", ""),
            "project_id": request.args.get("project_id", ""),
        }
        return _proxy_get("/api/v1/deployment-governance/plans", action="deployment_governance_listed", query=query)
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    body.setdefault("created_by", _actor_label())
    return _proxy_write(
        "POST",
        "/api/v1/deployment-governance/plans",
        action="deployment_governance_plan_created",
        json_body=body,
    )


@ai_coding_api.route("/deployment-governance/plans/<plan_id>", methods=["GET"])
def deployment_governance_plan_detail(plan_id: str):
    return _proxy_get(
        f"/api/v1/deployment-governance/plans/{plan_id}",
        action="deployment_governance_plan_viewed",
        plan_id=plan_id,
    )


@ai_coding_api.route("/deployment-governance/plans/<plan_id>/generate", methods=["POST"])
def deployment_governance_plan_generate(plan_id: str):
    return _proxy_write(
        "POST",
        f"/api/v1/deployment-governance/plans/{plan_id}/generate",
        action="deployment_governance_plan_generated",
        query={"actor": _actor_label()},
        plan_id=plan_id,
    )


@ai_coding_api.route("/deployment-governance/plans/<plan_id>/approve", methods=["POST"])
def deployment_governance_plan_approve(plan_id: str):
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy_write(
        "POST",
        f"/api/v1/deployment-governance/plans/{plan_id}/approve",
        action="deployment_governance_plan_approved",
        json_body=body,
        plan_id=plan_id,
    )


@ai_coding_api.route("/deployment-governance/plans/<plan_id>/reject", methods=["POST"])
def deployment_governance_plan_reject(plan_id: str):
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy_write(
        "POST",
        f"/api/v1/deployment-governance/plans/{plan_id}/reject",
        action="deployment_governance_plan_rejected",
        json_body=body,
        plan_id=plan_id,
    )


@ai_coding_api.route("/deployment-governance/plans/<plan_id>/cancel", methods=["POST"])
def deployment_governance_plan_cancel(plan_id: str):
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy_write(
        "POST",
        f"/api/v1/deployment-governance/plans/{plan_id}/cancel",
        action="deployment_governance_plan_cancelled",
        json_body=body,
        plan_id=plan_id,
    )


@ai_coding_api.route("/deployment-governance/plans/<plan_id>/audit", methods=["GET"])
def deployment_governance_plan_audit(plan_id: str):
    return _proxy_get(
        f"/api/v1/deployment-governance/plans/{plan_id}/audit",
        action="deployment_governance_audit_viewed",
        plan_id=plan_id,
    )


@ai_coding_api.route("/engineering-memory/search", methods=["GET"])
def engineering_memory_search():
    query = {
        "q": request.args.get("q", ""),
        "project_id": request.args.get("project_id", ""),
        "category": request.args.get("category", ""),
        "tag": request.args.get("tag", ""),
        "severity": request.args.get("severity", ""),
        "include_archived": request.args.get("include_archived", ""),
        "limit": request.args.get("limit", "30"),
        "actor": _actor_label(),
    }
    return _proxy_get("/api/v1/engineering-memory/search", action="memory_search_performed", query=query)


@ai_coding_api.route("/projects/<project_id>/memory", methods=["GET", "POST"])
def project_memory(project_id: str):
    if request.method == "GET":
        query = {
            "include_archived": request.args.get("include_archived", ""),
            "limit": request.args.get("limit", "50"),
        }
        return _proxy_get(
            f"/api/v1/projects/{project_id}/memory",
            action="engineering_memory_viewed",
            query=query,
            project_id=project_id,
        )
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy_write(
        "POST",
        f"/api/v1/projects/{project_id}/memory",
        action="engineering_memory_created",
        json_body=body,
        project_id=project_id,
    )


@ai_coding_api.route("/memory/<memory_id>", methods=["PATCH"])
def project_memory_update(memory_id: str):
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy_write(
        "PATCH",
        f"/api/v1/memory/{memory_id}",
        action="engineering_memory_updated",
        json_body=body,
        memory_id=memory_id,
    )


@ai_coding_api.route("/projects/<project_id>/intelligence-summary", methods=["GET"])
def project_intelligence_summary(project_id: str):
    return _proxy_get(
        f"/api/v1/projects/{project_id}/intelligence-summary",
        action="project_intelligence_viewed",
        query={"actor": _actor_label()},
        project_id=project_id,
    )


@ai_coding_api.route("/projects/<project_id>/execution-context", methods=["GET"])
def project_execution_context(project_id: str):
    query = {
        "execution_id": request.args.get("execution_id", ""),
        "limit": request.args.get("limit", "8"),
        "actor": _actor_label(),
    }
    for key in ("tag", "category"):
        values = request.args.getlist(key)
        if values:
            query[key] = values
    return _proxy_get(
        f"/api/v1/projects/{project_id}/execution-context",
        action="execution_context_generated",
        query=query,
        project_id=project_id,
    )


@ai_coding_api.route("/projects/<project_id>/profile", methods=["GET", "PATCH"])
def project_profile(project_id: str):
    if request.method == "GET":
        return _proxy_get(
            f"/api/v1/projects/{project_id}/profile",
            action="project_intelligence_viewed",
            project_id=project_id,
        )
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy_write(
        "PATCH",
        f"/api/v1/projects/{project_id}/profile",
        action="engineering_memory_updated",
        json_body=body,
        project_id=project_id,
    )


@ai_coding_api.route("/projects/<project_id>/incidents", methods=["GET", "POST"])
def project_incidents(project_id: str):
    if request.method == "GET":
        return _proxy_get(
            f"/api/v1/projects/{project_id}/incidents",
            action="incident_memory_viewed",
            query={"limit": request.args.get("limit", "20")},
            project_id=project_id,
        )
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy_write(
        "POST",
        f"/api/v1/projects/{project_id}/incidents",
        action="incident_memory_created",
        json_body=body,
        project_id=project_id,
    )


@ai_coding_api.route("/task-plans", methods=["GET", "POST"])
def task_plans():
    if request.method == "GET":
        query = {
            "limit": request.args.get("limit", "50"),
            "status": request.args.get("status", ""),
            "project_id": request.args.get("project_id", ""),
            "include_archived": request.args.get("include_archived", ""),
        }
        return _proxy_get("/api/v1/task-plans", action="task_plan_listed", query=query)
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    body.setdefault("created_by", _actor_label())
    return _proxy_write(
        "POST",
        "/api/v1/task-plans",
        action="task_plan_created",
        json_body=body,
    )


@ai_coding_api.route("/task-plans/<plan_id>", methods=["GET"])
def task_plan_detail(plan_id: str):
    return _proxy_get(
        f"/api/v1/task-plans/{plan_id}",
        action="task_plan_viewed",
        query={"actor": _actor_label()},
        plan_id=plan_id,
    )


@ai_coding_api.route("/task-plans/<plan_id>/generate", methods=["POST"])
def task_plan_generate(plan_id: str):
    return _proxy_write(
        "POST",
        f"/api/v1/task-plans/{plan_id}/generate",
        action="task_plan_generated",
        query={"actor": _actor_label()},
        plan_id=plan_id,
    )


@ai_coding_api.route("/task-plans/<plan_id>/approve", methods=["POST"])
def task_plan_approve(plan_id: str):
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy_write(
        "POST",
        f"/api/v1/task-plans/{plan_id}/approve",
        action="task_plan_approved",
        json_body=body,
        plan_id=plan_id,
    )


@ai_coding_api.route("/task-plans/<plan_id>/reject", methods=["POST"])
def task_plan_reject(plan_id: str):
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy_write(
        "POST",
        f"/api/v1/task-plans/{plan_id}/reject",
        action="task_plan_rejected",
        json_body=body,
        plan_id=plan_id,
    )


@ai_coding_api.route("/task-plans/<plan_id>/phases/<phase_id>/approve", methods=["POST"])
def task_plan_phase_approve(plan_id: str, phase_id: str):
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy_write(
        "POST",
        f"/api/v1/task-plans/{plan_id}/phases/{phase_id}/approve",
        action="task_phase_approved",
        json_body=body,
        plan_id=plan_id,
        phase_id=phase_id,
    )


@ai_coding_api.route("/task-plans/<plan_id>/export", methods=["GET"])
def task_plan_export(plan_id: str):
    fmt = request.args.get("fmt", "markdown")
    status, body, content_type = fetch_upstream_raw(
        f"/api/v1/task-plans/{plan_id}/export",
        query={"fmt": fmt, "actor": _actor_label()},
    )
    _audit("task_plan_exported", plan_id=plan_id, upstream_status=status, format=fmt)
    if status != 200:
        try:
            payload = json.loads(body)
        except Exception:
            payload = {"error": "export_failed", "detail": body[:500]}
        return jsonify(payload), status
    filename = f"task-plan-{plan_id}.{ 'md' if fmt == 'markdown' else 'json' }"
    return Response(
        body,
        status=status,
        mimetype=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@ai_coding_api.route("/task-plans/<plan_id>/phases/<phase_id>/prompt", methods=["GET"])
def task_plan_phase_prompt(plan_id: str, phase_id: str):
    return _proxy_get(
        f"/api/v1/task-plans/{plan_id}/phases/{phase_id}/prompt",
        action="task_phase_prompt_copied",
        query={"actor": _actor_label()},
        plan_id=plan_id,
        phase_id=phase_id,
    )


@ai_coding_api.route("/task-plans/<plan_id>/create-execution", methods=["POST"])
def task_plan_create_execution(plan_id: str):
    return _proxy_write(
        "POST",
        f"/api/v1/task-plans/{plan_id}/create-execution",
        action="execution_program_created",
        query={"actor": _actor_label()},
        plan_id=plan_id,
    )


@ai_coding_api.route("/execution-programs", methods=["GET", "POST"])
def execution_programs():
    if request.method == "GET":
        query = {
            "limit": request.args.get("limit", "50"),
            "status": request.args.get("status", ""),
            "project_id": request.args.get("project_id", ""),
            "include_archived": request.args.get("include_archived", ""),
        }
        return _proxy_get("/api/v1/execution-programs", action="execution_program_listed", query=query)
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy_write("POST", "/api/v1/execution-programs", action="execution_program_created", json_body=body)


@ai_coding_api.route("/execution-programs/<program_id>", methods=["GET"])
def execution_program_detail(program_id: str):
    return _proxy_get(
        f"/api/v1/execution-programs/{program_id}",
        action="execution_program_viewed",
        query={"actor": _actor_label()},
        program_id=program_id,
    )


@ai_coding_api.route("/execution-programs/<program_id>/start", methods=["POST"])
def execution_program_start(program_id: str):
    return _proxy_write(
        "POST",
        f"/api/v1/execution-programs/{program_id}/start",
        action="execution_program_started",
        query={"actor": _actor_label()},
        program_id=program_id,
    )


@ai_coding_api.route("/execution-programs/<program_id>/cancel", methods=["POST"])
def execution_program_cancel(program_id: str):
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy_write(
        "POST",
        f"/api/v1/execution-programs/{program_id}/cancel",
        action="execution_program_cancelled",
        json_body=body,
        program_id=program_id,
    )


@ai_coding_api.route("/execution-programs/<program_id>/phases/<phase_id>/approve", methods=["POST"])
def execution_program_phase_approve(program_id: str, phase_id: str):
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy_write(
        "POST",
        f"/api/v1/execution-programs/{program_id}/phases/{phase_id}/approve",
        action="execution_phase_approved",
        json_body=body,
        program_id=program_id,
        phase_id=phase_id,
    )


@ai_coding_api.route("/execution-programs/<program_id>/phases/<phase_id>/continue", methods=["POST"])
def execution_program_phase_continue(program_id: str, phase_id: str):
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy_write(
        "POST",
        f"/api/v1/execution-programs/{program_id}/phases/{phase_id}/continue",
        action="execution_phase_started",
        json_body=body,
        program_id=program_id,
        phase_id=phase_id,
    )


@ai_coding_api.route("/execution-programs/<program_id>/journal", methods=["GET"])
def execution_program_journal(program_id: str):
    return _proxy_get(
        f"/api/v1/execution-programs/{program_id}/journal",
        action="execution_journal_created",
        program_id=program_id,
    )


@ai_coding_api.route("/execution-programs/<program_id>/archive", methods=["POST"])
def execution_program_archive(program_id: str):
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy_write(
        "POST",
        f"/api/v1/execution-programs/{program_id}/archive",
        action="execution_program_archived",
        json_body=body,
        program_id=program_id,
    )


@ai_coding_api.route("/execution-programs/<program_id>/restore", methods=["POST"])
def execution_program_restore(program_id: str):
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy_write(
        "POST",
        f"/api/v1/execution-programs/{program_id}/restore",
        action="execution_program_restored",
        json_body=body,
        program_id=program_id,
    )


@ai_coding_api.route("/task-plans/<plan_id>/archive", methods=["POST"])
def task_plan_archive(plan_id: str):
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy_write(
        "POST",
        f"/api/v1/task-plans/{plan_id}/archive",
        action="task_plan_archived",
        json_body=body,
        plan_id=plan_id,
    )


@ai_coding_api.route("/task-plans/<plan_id>/restore", methods=["POST"])
def task_plan_restore(plan_id: str):
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy_write(
        "POST",
        f"/api/v1/task-plans/{plan_id}/restore",
        action="task_plan_restored",
        json_body=body,
        plan_id=plan_id,
    )


@ai_coding_api.route("/reviews/<execution_id>/archive", methods=["POST"])
def review_archive(execution_id: str):
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy_write(
        "POST",
        f"/api/v1/reviews/{execution_id}/archive",
        action="review_archived",
        json_body=body,
        execution_id=execution_id,
    )


@ai_coding_api.route("/reviews/<execution_id>/restore", methods=["POST"])
def review_restore(execution_id: str):
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy_write(
        "POST",
        f"/api/v1/reviews/{execution_id}/restore",
        action="review_restored",
        json_body=body,
        execution_id=execution_id,
    )


@ai_coding_api.route("/build-runs", methods=["GET", "POST"])
def build_runs_collection():
    if request.method == "GET":
        query: dict[str, str] = {"limit": request.args.get("limit", "50")}
        if request.args.get("project_id"):
            query["project_id"] = request.args.get("project_id", "").strip()
        if request.args.get("status"):
            query["status"] = request.args.get("status", "").strip()
        return _proxy_get("/api/v1/build-runs", action="build_runs_listed", query=query)
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy_write(
        "POST",
        "/api/v1/build-runs",
        action="build_run_created",
        json_body=body,
    )


@ai_coding_api.route("/build-runs/<run_id>", methods=["GET"])
def build_run_detail(run_id: str):
    return _proxy_get(
        f"/api/v1/build-runs/{run_id}",
        action="build_run_viewed",
        run_id=run_id,
    )


@ai_coding_api.route("/build-runs/<run_id>/operator-continue", methods=["POST"])
def build_run_operator_continue(run_id: str):
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy_write(
        "POST",
        f"/api/v1/build-runs/{run_id}/operator-continue",
        action="build_run_operator_continue",
        json_body=body,
        run_id=run_id,
    )


@ai_coding_api.route("/build-runs/<run_id>/advance", methods=["POST"])
def build_run_advance(run_id: str):
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy_write(
        "POST",
        f"/api/v1/build-runs/{run_id}/advance",
        action="build_run_advanced",
        json_body=body,
        run_id=run_id,
    )


@ai_coding_api.route("/build-runs/<run_id>/approve", methods=["POST"])
def build_run_approve(run_id: str):
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy_write(
        "POST",
        f"/api/v1/build-runs/{run_id}/approve",
        action="build_run_approved",
        json_body=body,
        run_id=run_id,
    )


@ai_coding_api.route("/build-runs/<run_id>/decline", methods=["POST"])
def build_run_decline(run_id: str):
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy_write(
        "POST",
        f"/api/v1/build-runs/{run_id}/decline",
        action="build_run_declined",
        json_body=body,
        run_id=run_id,
    )


@ai_coding_api.route("/build-runs/<run_id>/feedback", methods=["POST"])
def build_run_feedback(run_id: str):
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    body.setdefault("source", "operator_pre_manual_test")
    return _proxy_write(
        "POST",
        f"/api/v1/build-runs/{run_id}/feedback",
        action="build_run_feedback",
        json_body=body,
        run_id=run_id,
    )


@ai_coding_api.route("/build-runs/<run_id>/cancel", methods=["POST"])
def build_run_cancel(run_id: str):
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy_write(
        "POST",
        f"/api/v1/build-runs/{run_id}/cancel",
        action="build_run_cancelled",
        json_body=body,
        run_id=run_id,
    )


@ai_coding_api.route("/build-runs/<run_id>/journal", methods=["GET"])
def build_run_journal(run_id: str):
    return _proxy_get(
        f"/api/v1/build-runs/{run_id}/journal",
        action="build_run_journal_viewed",
        run_id=run_id,
    )


@ai_coding_api.route("/build-runs/<run_id>/workspace/prepare", methods=["POST"])
def build_run_prepare_workspace(run_id: str):
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy_write(
        "POST",
        f"/api/v1/build-runs/{run_id}/workspace/prepare",
        action="build_run_prepare_workspace",
        json_body=body,
        run_id=run_id,
    )


@ai_coding_api.route("/build-runs/<run_id>/execute", methods=["POST"])
def build_run_execute(run_id: str):
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy_write(
        "POST",
        f"/api/v1/build-runs/{run_id}/execute",
        action="build_run_execute",
        json_body=body,
        run_id=run_id,
    )


@ai_coding_api.route("/build-runs/<run_id>/jobs/execute", methods=["POST"])
def build_run_jobs_execute(run_id: str):
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy_write(
        "POST",
        f"/api/v1/build-runs/{run_id}/jobs/execute",
        action="build_run_jobs_execute",
        json_body=body,
        run_id=run_id,
    )


@ai_coding_api.route("/build-runs/<run_id>/jobs/auto-fix", methods=["POST"])
def build_run_jobs_auto_fix(run_id: str):
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy_write(
        "POST",
        f"/api/v1/build-runs/{run_id}/jobs/auto-fix",
        action="build_run_jobs_auto_fix",
        json_body=body,
        run_id=run_id,
    )


@ai_coding_api.route("/build-runs/<run_id>/jobs/dry-run-package", methods=["POST"])
def build_run_jobs_dry_run_package(run_id: str):
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy_write(
        "POST",
        f"/api/v1/build-runs/{run_id}/jobs/dry-run-package",
        action="build_run_jobs_dry_run_package",
        json_body=body,
        run_id=run_id,
    )


@ai_coding_api.route("/build-runs/<run_id>/jobs", methods=["GET"])
def build_run_jobs_list(run_id: str):
    return _proxy_get(
        f"/api/v1/build-runs/{run_id}/jobs",
        action="build_run_jobs_list",
        run_id=run_id,
    )


@ai_coding_api.route("/jobs/worker-health", methods=["GET"])
def background_job_worker_health():
    return _proxy_get(
        "/api/v1/jobs/worker-health",
        action="background_job_worker_health",
    )


@ai_coding_api.route("/jobs/<job_id>", methods=["GET"])
def background_job_status(job_id: str):
    return _proxy_get(
        f"/api/v1/jobs/{job_id}",
        action="background_job_status",
    )


@ai_coding_api.route("/release-runs/<release_plan_id>/jobs/execute", methods=["POST"])
def release_run_jobs_execute(release_plan_id: str):
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy_write(
        "POST",
        f"/api/v1/release-runs/{release_plan_id}/jobs/execute",
        action="release_run_jobs_execute",
        json_body=body,
    )


@ai_coding_api.route("/build-runs/<run_id>/auto-fix", methods=["POST"])
def build_run_auto_fix(run_id: str):
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy_write(
        "POST",
        f"/api/v1/build-runs/{run_id}/auto-fix",
        action="build_run_auto_fix",
        json_body=body,
        run_id=run_id,
    )


@ai_coding_api.route("/release-runs/<release_plan_id>/execute", methods=["POST"])
def release_run_execute(release_plan_id: str):
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy_write(
        "POST",
        f"/api/v1/release-runs/{release_plan_id}/execute",
        action="release_run_execute",
        json_body=body,
    )


@ai_coding_api.route("/build-runs/<run_id>/dry-run-package", methods=["POST"])
def build_run_dry_run_package(run_id: str):
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy_write(
        "POST",
        f"/api/v1/build-runs/{run_id}/dry-run-package",
        action="build_run_dry_run_package",
        json_body=body,
        run_id=run_id,
    )


@ai_coding_api.route("/build-runs/<run_id>/prepare-release", methods=["POST"])
def build_run_prepare_release(run_id: str):
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy_write(
        "POST",
        f"/api/v1/build-runs/{run_id}/prepare-release",
        action="build_run_prepare_release",
        json_body=body,
        run_id=run_id,
    )


@ai_coding_api.route("/build-runs/<run_id>/approve-release", methods=["POST"])
def build_run_approve_release(run_id: str):
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy_write(
        "POST",
        f"/api/v1/build-runs/{run_id}/approve-release",
        action="build_run_approve_release",
        json_body=body,
        run_id=run_id,
    )


@ai_coding_api.route("/build-runs/<run_id>/run-release", methods=["POST"])
def build_run_run_release(run_id: str):
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy_write(
        "POST",
        f"/api/v1/build-runs/{run_id}/run-release",
        action="build_run_run_release",
        json_body=body,
        run_id=run_id,
    )


@ai_coding_api.route("/build-runs/<run_id>/rollback-release", methods=["POST"])
def build_run_rollback_release(run_id: str):
    body = request.get_json(silent=True) or {}
    body.setdefault("actor", _actor_label())
    return _proxy_write(
        "POST",
        f"/api/v1/build-runs/{run_id}/rollback-release",
        action="build_run_rollback_release",
        json_body=body,
        run_id=run_id,
    )


@ai_coding_api.route("/build-runs/<run_id>/release-status", methods=["GET"])
def build_run_release_status(run_id: str):
    return _proxy_get(
        f"/api/v1/build-runs/{run_id}/release-status",
        action="build_run_release_status",
        run_id=run_id,
    )


@ai_coding_api.route("/notifications", methods=["GET"])
def ai_coding_notifications_list():
    unread = request.args.get("unread_only", "false").lower() == "true"
    limit = request.args.get("limit", "50")
    query = {"unread_only": "true" if unread else "false", "limit": limit}
    return _proxy_get("/api/v1/notifications", action="ai_coding_notifications_list", query=query)


@ai_coding_api.route("/notifications/<notification_id>/read", methods=["POST"])
def ai_coding_notification_mark_read(notification_id: str):
    return _proxy_write(
        "POST",
        f"/api/v1/notifications/{notification_id}/read",
        action="ai_coding_notification_mark_read",
    )


@ai_coding_api.route("/analytics/factory-health", methods=["GET"])
def analytics_factory_health():
    return _proxy_get("/api/v1/analytics/factory-health", action="analytics_factory_health")


@ai_coding_api.route("/analytics/factory", methods=["GET"])
def analytics_factory():
    return _proxy_get("/api/v1/analytics/factory", action="analytics_factory")


@ai_coding_api.route("/analytics/builds", methods=["GET"])
def analytics_builds():
    return _proxy_get("/api/v1/analytics/builds", action="analytics_builds")


@ai_coding_api.route("/analytics/workers", methods=["GET"])
def analytics_workers():
    return _proxy_get("/api/v1/analytics/workers", action="analytics_workers")


@ai_coding_api.route("/analytics/projects", methods=["GET"])
def analytics_projects():
    return _proxy_get("/api/v1/analytics/projects", action="analytics_projects")


@ai_coding_api.route("/analytics/releases", methods=["GET"])
def analytics_releases():
    return _proxy_get("/api/v1/analytics/releases", action="analytics_releases")
