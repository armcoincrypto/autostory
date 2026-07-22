"""
Tests for admin API authentication: fail-closed, token validation, constant-time compare.

Probe endpoint: GET /api/v1/targets (live scheduler API). Auth deny status is 401.
"""
import os
from unittest.mock import patch


PROTECTED_API = "/api/v1/targets"


def test_admin_api_no_token_denied_when_token_configured():
    """No X-Admin-Token header => 401 when DASHBOARD_ADMIN_TOKEN is set."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with patch.dict(os.environ, {"DASHBOARD_ADMIN_TOKEN": "correct-secret-token"}, clear=False):
        r = client.get(PROTECTED_API)
        assert r.status_code == 401
        data = r.get_json() or {}
        assert "unauthorized" in (data.get("error") or "").lower()


def test_admin_api_wrong_token_denied():
    """Wrong X-Admin-Token => 401."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with patch.dict(os.environ, {"DASHBOARD_ADMIN_TOKEN": "correct-secret-token"}, clear=False):
        r = client.get(
            PROTECTED_API,
            headers={"X-Admin-Token": "wrong-token"},
        )
        assert r.status_code == 401


def test_admin_api_correct_token_allowed():
    """Correct X-Admin-Token => not 401 (200 or 500 if DB/files missing)."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with patch.dict(os.environ, {"DASHBOARD_ADMIN_TOKEN": "correct-secret-token"}, clear=False):
        r = client.get(
            PROTECTED_API,
            headers={"X-Admin-Token": "correct-secret-token"},
        )
        assert r.status_code != 401, "Correct token must not be denied"
        assert r.status_code in (200, 500)


def test_admin_api_health_always_allowed():
    """GET /api/health must not require auth (exempt from require_admin_api)."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    # No token, no patches - /api/health is explicitly exempt
    r = client.get("/api/health")
    assert r.status_code == 200
    assert "healthy" in str(r.data).lower()


def test_admin_api_no_token_configured_production_denied():
    """No DASHBOARD_ADMIN_TOKEN configured => 401 (fail-closed)."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("DASHBOARD_ADMIN_TOKEN", None)
        with patch(
            "src.dashboard.auth_access._configured_admin_token",
            return_value="",
        ):
            r = client.get(PROTECTED_API)
            assert r.status_code == 401


def test_admin_api_allow_insecure_dev_bypass():
    """Insecure admin API bypass is intentionally absent; deny without token (fail-closed)."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with patch.dict(
        os.environ,
        {
            "DASHBOARD_ALLOW_INSECURE_ADMIN_API": "true",
            "ENVIRONMENT": "development",
        },
        clear=False,
    ):
        os.environ.pop("DASHBOARD_ADMIN_TOKEN", None)
        with patch(
            "src.dashboard.auth_access._configured_admin_token",
            return_value="",
        ):
            r = client.get(PROTECTED_API)
            assert r.status_code == 401


def test_scheduler_api_no_token_denied_when_token_configured():
    """No X-Admin-Token => 401 for /api/v1/* when DASHBOARD_ADMIN_TOKEN is set."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with patch.dict(os.environ, {"DASHBOARD_ADMIN_TOKEN": "token123"}, clear=False):
        r = client.get(PROTECTED_API)
        assert r.status_code == 401


def test_scheduler_api_correct_token_allowed():
    """Correct X-Admin-Token => allowed for /api/v1/*."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with patch.dict(os.environ, {"DASHBOARD_ADMIN_TOKEN": "token123"}, clear=False):
        r = client.get(PROTECTED_API, headers={"X-Admin-Token": "token123"})
        assert r.status_code != 401


def test_dashboard_unauthenticated_redirects_to_login():
    """Unauthenticated GET / or /accounts redirects to /login."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    for path in ["/", "/accounts", "/stories"]:
        r = client.get(path)
        assert r.status_code == 302
        assert "/login" in (r.headers.get("Location") or "")


def test_login_page_loads():
    """GET /login returns 200 with login form."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    r = client.get("/login")
    assert r.status_code == 200
    assert b"username" in r.data.lower() or b"log in" in r.data.lower()


def test_scheduler_api_no_token_denied():
    """GET /api/v1/targets without token => 401 when token configured."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with patch.dict(os.environ, {"DASHBOARD_ADMIN_TOKEN": "correct-secret-token"}, clear=False):
        r = client.get(PROTECTED_API)
        assert r.status_code == 401
        data = r.get_json() or {}
        assert "unauthorized" in (data.get("error") or "").lower()
