"""AI Coding dashboard routes — auth + server-side proxy."""
from __future__ import annotations

from unittest.mock import patch

import pytest

TOKEN = "ai-coding-test-token"


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DASHBOARD_ADMIN_TOKEN", TOKEN)
    from src.dashboard.app import create_app

    application = create_app()
    application.config["TESTING"] = True
    return application


def _headers() -> dict[str, str]:
    return {"X-Admin-Token": TOKEN}


def test_ai_coding_page_requires_auth(app) -> None:
    resp = app.test_client().get("/ai-coding")
    assert resp.status_code in (302, 401)


def test_ai_coding_page_renders_with_auth(app) -> None:
    resp = app.test_client().get("/ai-coding", headers=_headers())
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "AI Coding" in body
    assert "Active executions" in body
    assert "Recent builds" in body
    assert "/api/v1/ai-coding" in body
    assert "DASHBOARD_ADMIN_TOKEN" not in body


def test_ai_coding_api_requires_auth(app) -> None:
    resp = app.test_client().get("/api/v1/ai-coding/reviews/recent")
    assert resp.status_code == 401


def test_ai_coding_health_requires_auth(app) -> None:
    resp = app.test_client().get("/api/v1/ai-coding/health")
    assert resp.status_code == 401


@patch("src.dashboard.ai_coding_routes.fetch_upstream_health")
def test_ai_coding_health_proxy_success(mock_health, app) -> None:
    mock_health.return_value = (
        200,
        {
            "connected": True,
            "status": "ok",
            "latency_ms": 12,
            "app": "ai-software-factory",
            "services": {"database": "ok", "redis": "ok"},
        },
    )
    resp = app.test_client().get("/api/v1/ai-coding/health", headers=_headers())
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["connected"] is True
    assert "environment" not in payload


@patch("src.dashboard.ai_coding_routes.fetch_upstream_health")
def test_ai_coding_health_proxy_failure(mock_health, app) -> None:
    mock_health.return_value = (
        503,
        {"connected": False, "status": "unavailable", "error": "ai_coding_upstream_unavailable"},
    )
    resp = app.test_client().get("/api/v1/ai-coding/health", headers=_headers())
    assert resp.status_code == 503
    assert resp.get_json()["connected"] is False


@patch("src.dashboard.ai_coding_routes.fetch_upstream")
def test_ai_coding_proxy_recent(mock_fetch, app) -> None:
    mock_fetch.return_value = (200, {"items": [], "total": 0})
    resp = app.test_client().get(
        "/api/v1/ai-coding/reviews/recent?limit=10&verdict=approved",
        headers=_headers(),
    )
    assert resp.status_code == 200
    mock_fetch.assert_called_once()
    args, kwargs = mock_fetch.call_args
    assert args[0] == "/api/v1/reviews/recent"


@patch("src.dashboard.ai_coding_routes.fetch_upstream")
def test_ai_coding_proxy_recent_upstream_error(mock_fetch, app) -> None:
    mock_fetch.return_value = (502, {"error": "ai_coding_upstream_unavailable"})
    resp = app.test_client().get("/api/v1/ai-coding/reviews/recent", headers=_headers())
    assert resp.status_code == 502


@patch("src.dashboard.ai_coding_routes.fetch_upstream")
def test_ai_coding_executions_proxy(mock_fetch, app) -> None:
    mock_fetch.return_value = (200, {"items": [], "total": 0, "active_count": 0})
    resp = app.test_client().get("/api/v1/ai-coding/executions/recent", headers=_headers())
    assert resp.status_code == 200
    args, _ = mock_fetch.call_args
    assert args[0] == "/api/v1/action-console/executions/recent"


@patch("src.dashboard.ai_coding_routes.fetch_upstream")
def test_ai_coding_execution_logs_proxy(mock_fetch, app) -> None:
    mock_fetch.return_value = (200, {"execution_id": "x", "sections": [], "redacted": True})
    resp = app.test_client().get("/api/v1/ai-coding/executions/exec-1/logs", headers=_headers())
    assert resp.status_code == 200
    assert resp.get_json()["redacted"] is True


@patch("src.dashboard.ai_coding_routes.fetch_upstream")
def test_ai_coding_builds_proxy(mock_fetch, app) -> None:
    mock_fetch.return_value = (200, {"items": [], "total": 0})
    resp = app.test_client().get("/api/v1/ai-coding/builds/recent", headers=_headers())
    assert resp.status_code == 200
    args, _ = mock_fetch.call_args
    assert args[0] == "/api/v1/action-console/builds/recent"


def test_ai_coding_audit_event_requires_auth(app) -> None:
    resp = app.test_client().post("/api/v1/ai-coding/audit", json={"action": "ai_coding_fix_copied"})
    assert resp.status_code == 401


@patch("src.dashboard.ai_coding_routes.fetch_upstream")
def test_ai_coding_memory_search_proxy(mock_fetch, app) -> None:
    mock_fetch.return_value = (200, [])
    resp = app.test_client().get(
        "/api/v1/ai-coding/engineering-memory/search?project_id=abc",
        headers=_headers(),
    )
    assert resp.status_code == 200
    assert mock_fetch.call_args.args[0] == "/api/v1/engineering-memory/search"


def test_ai_coding_intelligence_page_renders(app) -> None:
    resp = app.test_client().get("/ai-coding/projects/abc/intelligence", headers=_headers())
    assert resp.status_code == 200
    assert "Project Intelligence" in resp.get_data(as_text=True)
    assert "Memory Explorer" in resp.get_data(as_text=True)


def test_ai_coding_planner_page_requires_auth(app) -> None:
    resp = app.test_client().get("/ai-coding/planner")
    assert resp.status_code in (302, 401)


def test_ai_coding_planner_page_renders(app) -> None:
    resp = app.test_client().get("/ai-coding/planner", headers=_headers())
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Advanced Task Planner" in body
    assert "Technical Task Planner" not in body
    assert "Advanced developer tool" in body
    assert "Back to Simple AI Coding Home" in body
    assert "For debugging and manual planning only" in body
    assert "Normal build flow starts from AI Coding Home" in body
    assert "planner-advanced-details" in body
    assert 'id="planner-advanced-details" open' not in body
    assert body.index("planner-advanced-details") < body.index("ai-code-wizard-panel")
    assert "Create Plan" in body
    assert "Phase Breakdown" in body
    assert "Create Execution Program" in body


@patch("src.dashboard.ai_coding_routes.fetch_upstream_method")
def test_ai_coding_task_plans_proxy(mock_write, app) -> None:
    mock_write.return_value = (201, {"id": "plan-1", "status": "draft"})
    resp = app.test_client().post(
        "/api/v1/ai-coding/task-plans",
        headers=_headers(),
        json={"project_id": "abc", "title": "Test", "goal": "Improve UX"},
    )
    assert resp.status_code == 201
    mock_write.assert_called_once()


@patch("src.dashboard.ai_coding_routes.fetch_upstream")
def test_ai_coding_task_plans_list_proxy(mock_fetch, app) -> None:
    mock_fetch.return_value = (200, [])
    resp = app.test_client().get("/api/v1/ai-coding/task-plans", headers=_headers())
    assert resp.status_code == 200
    assert mock_fetch.call_args.args[0] == "/api/v1/task-plans"


def test_ai_coding_task_plans_api_requires_auth(app) -> None:
    resp = app.test_client().get("/api/v1/ai-coding/task-plans")
    assert resp.status_code == 401


@patch("src.dashboard.ai_coding_routes.fetch_upstream")
def test_ai_coding_phase_prompt_proxy(mock_fetch, app) -> None:
    mock_fetch.return_value = (200, {"prompt": "Act as a senior production engineer"})
    resp = app.test_client().get(
        "/api/v1/ai-coding/task-plans/plan-1/phases/phase-1/prompt",
        headers=_headers(),
    )
    assert resp.status_code == 200
    assert "prompt" in resp.get_json()


@patch("src.dashboard.ai_coding_routes.fetch_upstream_method")
def test_ai_coding_create_execution_proxy(mock_write, app) -> None:
    mock_write.return_value = (
        201,
        {
            "execution_program_id": "prog-1",
            "plan_id": "plan-1",
            "status": "ready",
            "message": "Execution program created",
        },
    )
    resp = app.test_client().post(
        "/api/v1/ai-coding/task-plans/plan-1/create-execution",
        headers=_headers(),
    )
    assert resp.status_code == 201
    assert resp.get_json()["execution_program_id"] == "prog-1"


def test_ai_coding_executions_page_requires_auth(app) -> None:
    resp = app.test_client().get("/ai-coding/executions")
    assert resp.status_code in (302, 401)


def test_ai_coding_executions_page_renders(app) -> None:
    resp = app.test_client().get("/ai-coding/executions", headers=_headers())
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "AI Workflows" in body
    assert "Workflow timeline" in body
    assert "Activity log" in body


@patch("src.dashboard.ai_coding_routes.fetch_upstream")
def test_ai_coding_execution_programs_proxy(mock_fetch, app) -> None:
    mock_fetch.return_value = (200, [])
    resp = app.test_client().get("/api/v1/ai-coding/execution-programs", headers=_headers())
    assert resp.status_code == 200
    assert mock_fetch.call_args.args[0] == "/api/v1/execution-programs"


def test_ai_coding_execution_programs_api_requires_auth(app) -> None:
    resp = app.test_client().get("/api/v1/ai-coding/execution-programs")
    assert resp.status_code == 401


def test_ai_coding_deployments_page_requires_auth(app) -> None:
    resp = app.test_client().get("/ai-coding/deployments")
    assert resp.status_code in (302, 401)


def test_ai_coding_deployments_page_renders(app) -> None:
    resp = app.test_client().get("/ai-coding/deployments", headers=_headers())
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Deployment Governance" in body
    assert "Validation Checklist" in body
    assert "Rollback Checklist" in body
    assert "dep-approve" in body
    assert "dep-generate" in body
    assert "Deploy" not in body or "Deployment Governance" in body
    assert "Merge" not in body
    assert "Release" not in body


@patch("src.dashboard.ai_coding_routes.fetch_upstream")
def test_ai_coding_deployments_list_proxy(mock_fetch, app) -> None:
    mock_fetch.return_value = (200, [])
    resp = app.test_client().get("/api/v1/ai-coding/deployment-governance/plans", headers=_headers())
    assert resp.status_code == 200
    assert mock_fetch.call_args.args[0] == "/api/v1/deployment-governance/plans"


@patch("src.dashboard.ai_coding_routes.fetch_upstream_method")
def test_ai_coding_deployments_generate_proxy(mock_write, app) -> None:
    mock_write.return_value = (200, {"id": "plan-1", "status": "generated"})
    resp = app.test_client().post(
        "/api/v1/ai-coding/deployment-governance/plans/plan-1/generate",
        headers=_headers(),
    )
    assert resp.status_code == 200
    assert mock_write.call_args.args[1] == "/api/v1/deployment-governance/plans/plan-1/generate"


def test_ai_coding_executions_page_has_copy_prompt(app) -> None:
    resp = app.test_client().get("/ai-coding/executions", headers=_headers())
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Copy Prompt" in body
    assert "resolvePhasePrompt" in body


def test_ai_coding_page_has_ops_overview(app) -> None:
    resp = app.test_client().get("/ai-coding", headers=_headers())
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Operations Overview" in body
    assert "ops-exec-running" in body
    assert "ops-gov-awaiting" in body
    assert "ops-review-pending" in body
    assert "ops-health-panel" in body


def test_ai_coding_audit_coverage_page_requires_auth(app) -> None:
    resp = app.test_client().get("/ai-coding/audit-coverage")
    assert resp.status_code in (302, 401)


def test_ai_coding_audit_coverage_page_renders(app) -> None:
    resp = app.test_client().get("/ai-coding/audit-coverage", headers=_headers())
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Audit Coverage Report" in body
    assert "deployment_governance" in body or "Deployment Governance" in body
    assert "auto-deploy" not in body.lower() or "does not" in body.lower()


def test_ai_coding_executions_page_has_timeline_icons(app) -> None:
    resp = app.test_client().get("/ai-coding/executions", headers=_headers())
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "phaseTimelineMeta" in body
    assert "stat-review" in body


def test_ai_coding_deployments_page_has_score_explanation(app) -> None:
    resp = app.test_client().get("/ai-coding/deployments", headers=_headers())
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Score Explanation" in body
    assert "renderScoreExplanation" in body


def test_ai_coding_memory_api_requires_auth(app) -> None:
    resp = app.test_client().get("/api/v1/ai-coding/projects/abc/memory")
    assert resp.status_code == 401


def test_ai_coding_audit_event_accepts_auth(app) -> None:
    resp = app.test_client().post(
        "/api/v1/ai-coding/audit",
        json={"action": "ai_coding_fix_copied", "execution_id": "exec-1"},
        headers=_headers(),
    )
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


@patch("src.dashboard.ai_coding_routes.fetch_upstream_method")
def test_ai_coding_execution_program_archive_proxy(mock_write, app) -> None:
    mock_write.return_value = (200, {"id": "prog-1", "status": "archived"})
    resp = app.test_client().post(
        "/api/v1/ai-coding/execution-programs/prog-1/archive",
        headers=_headers(),
        json={"reason": "cleanup"},
    )
    assert resp.status_code == 200
    assert mock_write.call_args.args[1] == "/api/v1/execution-programs/prog-1/archive"


@patch("src.dashboard.ai_coding_routes.fetch_upstream_method")
def test_ai_coding_task_plan_restore_proxy(mock_write, app) -> None:
    mock_write.return_value = (200, {"id": "plan-1", "status": "draft"})
    resp = app.test_client().post(
        "/api/v1/ai-coding/task-plans/plan-1/restore",
        headers=_headers(),
    )
    assert resp.status_code == 200
    assert mock_write.call_args.args[1] == "/api/v1/task-plans/plan-1/restore"


@patch("src.dashboard.ai_coding_routes.fetch_upstream_method")
def test_ai_coding_review_archive_proxy(mock_write, app) -> None:
    mock_write.return_value = (200, {"execution_id": "exec-1"})
    resp = app.test_client().post(
        "/api/v1/ai-coding/reviews/exec-1/archive",
        headers=_headers(),
        json={"reason": "noise"},
    )
    assert resp.status_code == 200
    assert mock_write.call_args.args[1] == "/api/v1/reviews/exec-1/archive"


def test_ai_coding_page_has_archive_controls(app) -> None:
    resp = app.test_client().get("/ai-coding", headers=_headers())
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "ai-code-show-archived" in body
    assert "data-archive-review" in body


def test_ai_coding_executions_page_has_archive_controls(app) -> None:
    resp = app.test_client().get("/ai-coding/executions", headers=_headers())
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "exec-show-archived" in body
    assert "exec-archive" in body
    assert "exec-restore" in body


def test_ai_coding_planner_page_has_archive_controls(app) -> None:
    resp = app.test_client().get("/ai-coding/planner", headers=_headers())
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "planner-show-archived" in body
    assert "planner-archive" in body
    assert "planner-restore" in body


@patch("src.dashboard.ai_coding_routes.fetch_upstream_method")
def test_ai_coding_execution_continue_proxy(mock_write, app) -> None:
    mock_write.return_value = (
        200,
        {
            "id": "6bb32259-0c47-4456-ab5c-1cd367a1c5d7",
            "status": "running",
            "phases": [{"id": "phase-1", "status": "running", "phase_number": 1}],
        },
    )
    resp = app.test_client().post(
        "/api/v1/ai-coding/execution-programs/prog-1/phases/phase-1/continue",
        headers=_headers(),
        json={"actor": "operator"},
    )
    assert resp.status_code == 200
    assert mock_write.call_args.args[1] == "/api/v1/execution-programs/prog-1/phases/phase-1/continue"


@patch("src.dashboard.ai_coding_routes.fetch_upstream_method")
def test_ai_coding_execution_continue_proxy_forwards_plain_error(mock_write, app) -> None:
    mock_write.return_value = (500, {"detail": "Internal Server Error"})
    resp = app.test_client().post(
        "/api/v1/ai-coding/execution-programs/prog-1/phases/phase-1/continue",
        headers=_headers(),
        json={},
    )
    assert resp.status_code == 500
    assert resp.get_json()["detail"] == "Internal Server Error"


@patch("src.dashboard.ai_coding_routes.fetch_upstream_method")
def test_ai_coding_execution_operator_review_proxy(mock_write, app) -> None:
    mock_write.return_value = (200, {"execution_id": "exec-1", "review_status": "approved"})
    resp = app.test_client().post(
        "/api/v1/ai-coding/executions/exec-1/operator-review",
        headers=_headers(),
        json={"decision": "approved", "summary": "Dry-run OK"},
    )
    assert resp.status_code == 200
    assert mock_write.call_args.args[1] == "/api/v1/executions/exec-1/operator-review"


def test_ai_coding_executions_page_review_gate_controls(app) -> None:
    resp = app.test_client().get("/ai-coding/executions", headers=_headers())
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "detail-review-gate" in body
    assert "exec-review-approve" in body
    assert "submitOperatorReview" in body


def test_ai_coding_executions_page_continue_has_error_handling(app) -> None:
    resp = app.test_client().get("/ai-coding/executions", headers=_headers())
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'id="exec-continue"' in body
    assert "friendlyExecError" in body
    assert "requireProgramAndPhase" in body




def test_ai_coding_planner_page_create_button_has_error_handling(app) -> None:
    resp = app.test_client().get("/ai-coding/planner", headers=_headers())
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'id="planner-create"' in body
    assert "friendlyPlannerError" in body
    assert "loadProjectDefault" in body
    assert "Project ID must be a valid UUID" in body


@patch("src.dashboard.ai_coding_routes.fetch_upstream_method")
def test_ai_coding_task_plans_create_proxy_forwards_validation_error(mock_write, app) -> None:
    mock_write.return_value = (
        422,
        {
            "detail": [
                {
                    "type": "uuid_parsing",
                    "loc": ["body", "project_id"],
                    "msg": "Input should be a valid UUID",
                }
            ]
        },
    )
    resp = app.test_client().post(
        "/api/v1/ai-coding/task-plans",
        headers=_headers(),
        json={"project_id": "", "title": "Test", "goal": "Improve UX"},
    )
    assert resp.status_code == 422
    detail = resp.get_json()["detail"]
    assert detail[0]["loc"] == ["body", "project_id"]


def test_ai_coding_simple_home_default(app) -> None:
    resp = app.test_client().get("/ai-coding", headers=_headers())
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "ai-operator-home" in body
    assert "ai_coding_operator_home.js" in body
    assert "Advanced / Developer Tools" in body
    assert "Review transparency" in body
    assert 'id="ai-dev-advanced-panel" open' not in body
    assert 'id="ai-dev-advanced-panel"' in body
    assert body.index("ai-operator-home") < body.index("ai-dev-advanced-panel")


def test_ai_coding_hides_technical_tables_by_default(app) -> None:
    resp = app.test_client().get("/ai-coding", headers=_headers())
    body = resp.get_data(as_text=True)
    assert "ai-code-rows" in body
    assert "ai-code-active-cards" in body
    dev_start = body.find("ai-dev-advanced-panel")
    assert dev_start > 0
    assert body.find("ai-code-rows") > dev_start
    assert body.find("Active executions") > dev_start or "ai-code-active-cards" in body[dev_start:]


def test_ai_coding_advanced_contains_ops_overview(app) -> None:
    resp = app.test_client().get("/ai-coding", headers=_headers())
    body = resp.get_data(as_text=True)
    assert "ops-overview" in body
    assert "Operations Overview" in body


def test_ai_coding_builds_simple_form_fields(app) -> None:
    from pathlib import Path

    resp = app.test_client().get("/ai-coding/builds", headers=_headers())
    body = resp.get_data(as_text=True)
    js = (
        Path(__file__).resolve().parents[1]
        / "src/dashboard/static/js/ai_coding_supervised_build.js"
    ).read_text(encoding="utf-8")
    assert "What should AI build" in js
    assert "Advanced options" in js
    assert "sb-project" in js
    assert "secret exposure" in js
    assert body.find("Advanced") >= 0


def test_ai_coding_hub_page_has_collapsed_autopilot(app) -> None:
    resp = app.test_client().get("/ai-coding", headers=_headers())
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "ai-code-wizard-panel" in body
    assert "ai_coding_operator_wizard.js" in body


@pytest.mark.parametrize(
    "path",
    ["/ai-coding/planner", "/ai-coding/executions", "/ai-coding/deployments"],
)
def test_ai_coding_pages_have_operator_autopilot(app, path: str) -> None:
    resp = app.test_client().get(path, headers=_headers())
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "ai-code-wizard-panel" in body
    assert "ai_coding_operator_wizard.js" in body


@patch("src.dashboard.ai_coding_routes.fetch_upstream_method")
def test_build_run_operator_continue_proxy(mock_write, app) -> None:
    mock_write.return_value = (
        200,
        {
            "status_label": "AI working",
            "current_step": "AI is working on the next step.",
            "primary_action_label": "Continue AI work",
            "needs_operator_input": False,
            "manual_test_required": False,
            "friendly_message": "",
            "safe_to_continue": True,
            "production_status_label": "AI is working",
            "completed_summary": "Task plan prepared · No production release yet",
            "current_work_summary": "AI is working on the next build step.",
            "next_step_summary": "Continue AI work",
            "why_waiting_summary": "Not waiting",
            "next_after_click_summary": "AI will run the next safe build step.",
            "production_release_summary": "Not released",
            "production_readiness_percent": 40,
            "production_checklist": [
                {"key": "discovery", "label": "Discovery", "status": "Waiting"},
                {"key": "implementation", "label": "Implementation", "status": "Waiting"},
                {"key": "verification", "label": "Verification", "status": "Waiting"},
                {"key": "manual_test", "label": "Manual test", "status": "Waiting"},
                {"key": "production_release", "label": "Production release", "status": "Not released"},
            ],
            "advanced": True,
            "run": {
                "id": "br-1",
                "project_id": "p-1",
                "task_title": "MVP",
                "task_goal": "Build",
                "status": "executing",
                "attempt_count": 0,
                "max_attempts": 3,
                "created_by": "operator",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
            },
        },
    )
    resp = app.test_client().post(
        "/api/v1/ai-coding/build-runs/br-1/operator-continue",
        headers=_headers(),
        json={"actor": "operator"},
    )
    assert resp.status_code == 200
    assert mock_write.call_args.args[1] == "/api/v1/build-runs/br-1/operator-continue"


@patch("src.dashboard.ai_coding_routes.fetch_upstream_method")
def test_build_run_create_proxy(mock_write, app) -> None:
    mock_write.return_value = (
        201,
        {
            "id": "br-1",
            "project_id": "p-1",
            "task_title": "MVP",
            "status": "draft",
            "attempt_count": 0,
            "max_attempts": 3,
        },
    )
    resp = app.test_client().post(
        "/api/v1/ai-coding/build-runs",
        headers=_headers(),
        json={
            "project_id": "00000000-0000-0000-0000-000000000001",
            "task_title": "MVP",
            "task_goal": "Build feature",
        },
    )
    assert resp.status_code == 201
    assert mock_write.call_args.args[1] == "/api/v1/build-runs"


def test_ai_coding_builds_page_operator_mode(app) -> None:
    from pathlib import Path

    resp = app.test_client().get("/ai-coding/builds", headers=_headers())
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Simple Operator" in body or "Build Task" in body
    assert "ai_coding_operator_mode.js" in body
    assert "ai_coding_supervised_build.js" in body
    assert "operator-trust-3" in body
    assert "ai-sb-detail" in body
    assert 'data-operator-mode="true"' not in body  # rendered client-side
    root = Path(__file__).resolve().parents[1]
    js = (root / "src/dashboard/static/js/ai_coding_supervised_build.js").read_text(encoding="utf-8")
    om_js = (root / "src/dashboard/static/js/ai_coding_operator_mode.js").read_text(encoding="utf-8")
    assert "op-advanced-details" in om_js
    assert "Ready for manual test" in js
    assert "sb-primary" in js
    assert "sb-decline-feedback" in js
    assert "Send feedback" in om_js
    routes_text = (root / "src/dashboard/ai_coding_routes.py").read_text(encoding="utf-8")
    assert "prepare-release" in routes_text
    assert "dry-run-package" in routes_text
    assert "release-runs/<release_plan_id>/execute" in routes_text
    assert "jobs/execute" in routes_text
    assert "/jobs/<job_id>" in routes_text
    assert "/build-runs/<run_id>/feedback" in routes_text
    assert 'id="sb-approve"' not in js or "sb-primary" in js  # single primary, not dual approve buttons in template


def test_operator_mode_advanced_collapsed_by_default(app) -> None:
    from pathlib import Path

    resp = app.test_client().get("/ai-coding/builds", headers=_headers())
    body = resp.get_data(as_text=True)
    js = (
        Path(__file__).resolve().parents[1]
        / "src/dashboard/static/js/ai_coding_supervised_build.js"
    ).read_text(encoding="utf-8")
    assert "<details" in body or "op-mode-advanced" in js
    assert 'op-advanced-details" open' not in js


def test_operator_mode_technical_ids_only_in_advanced_js(app) -> None:
    from pathlib import Path

    js = (
        Path(__file__).resolve().parents[1]
        / "src/dashboard/static/js/ai_coding_operator_mode.js"
    ).read_text(encoding="utf-8")
    assert "Build run:" in js
    assert "renderAdvancedDetails" in js


def test_ai_coding_hub_has_simple_operator_copy(app) -> None:
    resp = app.test_client().get("/ai-coding", headers=_headers())
    body = resp.get_data(as_text=True)
    assert "Idea → Build → Test → Approve → Release" in body
    assert "hub-flow-card" in body
    assert "hub-safety-chip" in body
    assert "ai_coding_operator_home.js" in body
    assert "operator-trust-3" in body
    assert "Production Console" not in body.split("ai-dev-advanced")[0]


def test_ai_coding_pages_no_unsafe_autopilot_controls(app) -> None:
    import re

    for path in ("/ai-coding", "/ai-coding/planner", "/ai-coding/executions", "/ai-coding/deployments", "/ai-coding/builds"):
        body = app.test_client().get(path, headers=_headers()).get_data(as_text=True)
        assert re.search(r'id="[^"]*auto[-_]?deploy', body, re.I) is None
        assert re.search(r'id="[^"]*auto[-_]?merge', body, re.I) is None
        assert re.search(r'id="[^"]*auto[-_]?release', body, re.I) is None
        assert re.search(r'id="[^"]*auto[-_]?send', body, re.I) is None
