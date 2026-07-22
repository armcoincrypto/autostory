from __future__ import annotations

from flask import Flask

from src.dashboard import app as app_module
from src.dashboard.auth_access import operator_api_authorized
from src.dashboard.routes import api


def auth_test_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = "test-only"
    app_module.login_manager.init_app(app)
    return app


def test_public_liveness_is_minimal() -> None:
    app = Flask(__name__)
    app.register_blueprint(api)

    response = app.test_client().get("/api/health")

    assert response.status_code == 200
    assert response.get_json() == {"status": "healthy"}
    assert "Access-Control-Allow-Origin" not in response.headers


def test_operator_auth_fails_closed_without_configured_token(monkeypatch) -> None:
    monkeypatch.delenv("DASHBOARD_ADMIN_TOKEN", raising=False)
    app = auth_test_app()

    with app.test_request_context(
        "/api/health/deep", headers={"X-Admin-Token": "untrusted"}
    ):
        assert operator_api_authorized() is False


def test_operator_auth_rejects_wrong_and_query_tokens(monkeypatch) -> None:
    monkeypatch.setenv("DASHBOARD_ADMIN_TOKEN", "correct-header-token")
    monkeypatch.setenv("DASHBOARD_ALLOW_QUERY_ADMIN_TOKEN", "true")
    app = auth_test_app()

    with app.test_request_context(
        "/api/health/deep?admin_token=correct-header-token",
        headers={"X-Admin-Token": "wrong"},
    ):
        assert operator_api_authorized() is False


def test_restricted_diagnostics_unauthorized_response_is_no_store(monkeypatch) -> None:
    monkeypatch.setenv("DASHBOARD_ADMIN_TOKEN", "configured")
    app = auth_test_app()
    app_module._ensure_p3_deep_health_route(app)

    response = app.test_client().get("/api/health/deep")

    assert response.status_code == 401
    assert response.get_json() == {"ok": False, "error": "unauthorized"}
    assert response.headers["Cache-Control"] == "no-store"
    assert "Access-Control-Allow-Origin" not in response.headers


def test_restricted_diagnostics_rate_limit_is_no_store(monkeypatch) -> None:
    monkeypatch.setenv("DASHBOARD_ADMIN_TOKEN", "configured")
    monkeypatch.setattr(app_module, "_DIAGNOSTIC_RATE_MAX", 0)
    app = auth_test_app()
    app_module._ensure_p3_deep_health_route(app)

    response = app.test_client().get(
        "/api/health/deep", headers={"X-Admin-Token": "configured"}
    )

    assert response.status_code == 429
    assert response.get_json() == {"ok": False, "error": "rate_limited"}
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Retry-After"] == "60"


def test_restricted_diagnostics_redacts_dependency_errors(monkeypatch) -> None:
    """Force a deep-health dependency failure and assert error detail is redacted."""
    monkeypatch.setenv("DASHBOARD_ADMIN_TOKEN", "configured")
    monkeypatch.setattr(app_module, "_DIAGNOSTIC_RATE_MAX", 30)
    app_module._diagnostic_rate_events.clear()
    app = auth_test_app()
    app_module._ensure_p3_deep_health_route(app)

    def _boom(*_args, **_kwargs):
        raise ModuleNotFoundError("No module named 'src.recovery.secret_internal_path'")

    monkeypatch.setattr("src.core.database.get_db_context", _boom)

    response = app.test_client().get(
        "/api/health/deep", headers={"X-Admin-Token": "configured"}
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "degraded"
    assert payload["queue_error"] == "dependency_query_failed"
    body = response.get_data(as_text=True)
    assert "No module named" not in body
    assert "secret_internal_path" not in body
    assert response.headers["Cache-Control"] == "no-store"
