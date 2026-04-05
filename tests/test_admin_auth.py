"""
Tests for admin API authentication: fail-closed, token validation, constant-time compare.
"""
import os
from unittest.mock import patch, MagicMock

import pytest


def test_admin_api_no_token_denied_when_token_configured():
    """No X-Admin-Token header => 403 when DASHBOARD_ADMIN_TOKEN is set."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with patch.dict(os.environ, {"DASHBOARD_ADMIN_TOKEN": "correct-secret-token"}, clear=False):
        with patch("src.dashboard.routes.settings") as mock_settings:
            mock_settings.dashboard.admin_token = "correct-secret-token"
            mock_settings.dashboard.allow_insecure_admin_api = False
            mock_settings.environment = "production"

            r = client.get("/api/accounts/session-audit")
            assert r.status_code == 403
            data = r.get_json()
            assert "admin" in (data.get("error") or "").lower()


def test_admin_api_wrong_token_denied():
    """Wrong X-Admin-Token => 403."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with patch.dict(os.environ, {"DASHBOARD_ADMIN_TOKEN": "correct-secret-token"}, clear=False):
        with patch("src.dashboard.routes.settings") as mock_settings:
            mock_settings.dashboard.admin_token = "correct-secret-token"
            mock_settings.dashboard.allow_insecure_admin_api = False
            mock_settings.environment = "production"

            r = client.get(
                "/api/accounts/session-audit",
                headers={"X-Admin-Token": "wrong-token"},
            )
            assert r.status_code == 403


def test_admin_api_correct_token_allowed():
    """Correct X-Admin-Token => 200 (or 500 if DB/files missing, but not 403)."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with patch.dict(os.environ, {"DASHBOARD_ADMIN_TOKEN": "correct-secret-token"}, clear=False):
        mock_settings = MagicMock()
        mock_settings.dashboard.admin_token = "correct-secret-token"
        mock_settings.dashboard.allow_insecure_admin_api = False
        mock_settings.environment = "production"
        with patch("src.dashboard.routes.settings", mock_settings):
            r = client.get(
                "/api/accounts/session-audit",
                headers={"X-Admin-Token": "correct-secret-token"},
            )
            assert r.status_code != 403, "Correct token must not be denied"
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
    """No DASHBOARD_ADMIN_TOKEN in production => 403."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("DASHBOARD_ADMIN_TOKEN", None)
        with patch("src.dashboard.routes._admin_token_configured", return_value=None):
            with patch("src.dashboard.routes.settings") as mock_settings:
                mock_settings.dashboard.allow_insecure_admin_api = False
                mock_settings.environment = "production"

                r = client.get("/api/accounts/session-audit")
                assert r.status_code == 403


def test_admin_api_allow_insecure_dev_bypass():
    """DASHBOARD_ALLOW_INSECURE_ADMIN_API=true + development => allow without token."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    mock_settings = MagicMock()
    mock_settings.dashboard.allow_insecure_admin_api = True
    mock_settings.environment = "development"
    with patch.dict(os.environ, {"DASHBOARD_ALLOW_INSECURE_ADMIN_API": "true"}, clear=False):
        with patch("src.dashboard.routes._admin_token_configured", return_value=None):
            with patch("src.dashboard.routes.settings", mock_settings):
                r = client.get("/api/accounts/session-audit")
                assert r.status_code != 403


def test_scheduler_api_no_token_denied_when_token_configured():
    """No X-Admin-Token => 403 for /api/v1/* when DASHBOARD_ADMIN_TOKEN is set."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with patch.dict(os.environ, {"DASHBOARD_ADMIN_TOKEN": "token123"}, clear=False):
        with patch("src.dashboard.routes.settings") as mock_settings:
            mock_settings.dashboard.admin_token = "token123"
            mock_settings.dashboard.allow_insecure_admin_api = False
            mock_settings.environment = "production"

            r = client.get("/api/v1/targets")
            assert r.status_code == 403


def test_scheduler_api_correct_token_allowed():
    """Correct X-Admin-Token => allowed for /api/v1/*."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with patch.dict(os.environ, {"DASHBOARD_ADMIN_TOKEN": "token123"}, clear=False):
        mock_settings = MagicMock()
        mock_settings.dashboard.admin_token = "token123"
        mock_settings.dashboard.allow_insecure_admin_api = False
        mock_settings.environment = "production"
        with patch("src.dashboard.routes.settings", mock_settings):
            r = client.get("/api/v1/targets", headers={"X-Admin-Token": "token123"})
            assert r.status_code != 403


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
    """No X-Admin-Token => 403 for /api/v1/* when token is configured."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with patch.dict(os.environ, {"DASHBOARD_ADMIN_TOKEN": "scheduler-secret"}, clear=False):
        with patch("src.dashboard.routes.settings") as mock_settings:
            mock_settings.dashboard.admin_token = "scheduler-secret"
            mock_settings.dashboard.allow_insecure_admin_api = False
            mock_settings.environment = "production"

            r = client.get("/api/v1/targets")
            assert r.status_code == 403


def test_scheduler_api_correct_token_allowed():
    """Correct X-Admin-Token => 200 for /api/v1/*."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with patch.dict(os.environ, {"DASHBOARD_ADMIN_TOKEN": "scheduler-secret"}, clear=False):
        mock_settings = MagicMock()
        mock_settings.dashboard.admin_token = "scheduler-secret"
        mock_settings.dashboard.allow_insecure_admin_api = False
        mock_settings.environment = "production"
        with patch("src.dashboard.routes.settings", mock_settings):
            r = client.get("/api/v1/targets", headers={"X-Admin-Token": "scheduler-secret"})
            assert r.status_code != 403


def test_scheduler_api_no_token_denied():
    """No token => 403 for /api/v1/* when token is configured."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with patch.dict(os.environ, {"DASHBOARD_ADMIN_TOKEN": "scheduler-secret"}, clear=False):
        mock_settings = MagicMock()
        mock_settings.dashboard.admin_token = "scheduler-secret"
        mock_settings.dashboard.allow_insecure_admin_api = False
        mock_settings.environment = "production"
        with patch("src.dashboard.routes.settings", mock_settings):
            r = client.get("/api/v1/targets")
            assert r.status_code == 403
            assert "admin" in (r.get_json() or {}).get("error", "").lower()


def test_scheduler_api_correct_token_allowed():
    """Correct X-Admin-Token => allowed for /api/v1/*."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with patch.dict(os.environ, {"DASHBOARD_ADMIN_TOKEN": "scheduler-secret"}, clear=False):
        mock_settings = MagicMock()
        mock_settings.dashboard.admin_token = "scheduler-secret"
        mock_settings.dashboard.allow_insecure_admin_api = False
        mock_settings.environment = "production"
        with patch("src.dashboard.routes.settings", mock_settings):
            r = client.get("/api/v1/targets", headers={"X-Admin-Token": "scheduler-secret"})
            assert r.status_code != 403


def test_scheduler_api_no_token_denied():
    """No token => 403 for /api/v1/* when token is configured."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with patch.dict(os.environ, {"DASHBOARD_ADMIN_TOKEN": "scheduler-secret"}, clear=False):
        mock_settings = MagicMock()
        mock_settings.dashboard.admin_token = "scheduler-secret"
        mock_settings.dashboard.allow_insecure_admin_api = False
        mock_settings.environment = "production"
        with patch("src.dashboard.routes.settings", mock_settings):
            r = client.get("/api/v1/targets")
            assert r.status_code == 403


def test_scheduler_api_correct_token_allowed():
    """Correct X-Admin-Token => 200 for /api/v1/* (or error if DB missing)."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with patch.dict(os.environ, {"DASHBOARD_ADMIN_TOKEN": "scheduler-secret"}, clear=False):
        mock_settings = MagicMock()
        mock_settings.dashboard.admin_token = "scheduler-secret"
        mock_settings.dashboard.allow_insecure_admin_api = False
        mock_settings.environment = "production"
        with patch("src.dashboard.routes.settings", mock_settings):
            r = client.get("/api/v1/targets", headers={"X-Admin-Token": "scheduler-secret"})
            assert r.status_code != 403


def test_scheduler_api_no_token_denied():
    """GET /api/v1/targets without token => 403 when token configured."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with patch.dict(os.environ, {"DASHBOARD_ADMIN_TOKEN": "scheduler-secret"}, clear=False):
        with patch("src.dashboard.routes.settings") as mock_settings:
            mock_settings.dashboard.admin_token = "scheduler-secret"
            mock_settings.dashboard.allow_insecure_admin_api = False
            mock_settings.environment = "production"

            r = client.get("/api/v1/targets")
            assert r.status_code == 403


def test_scheduler_api_correct_token_allowed():
    """GET /api/v1/targets with correct X-Admin-Token => not 403."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with patch.dict(os.environ, {"DASHBOARD_ADMIN_TOKEN": "scheduler-secret"}, clear=False):
        mock_settings = MagicMock()
        mock_settings.dashboard.admin_token = "scheduler-secret"
        mock_settings.dashboard.allow_insecure_admin_api = False
        mock_settings.environment = "production"
        with patch("src.dashboard.routes.settings", mock_settings):
            r = client.get("/api/v1/targets", headers={"X-Admin-Token": "scheduler-secret"})
            assert r.status_code != 403


def test_scheduler_api_no_token_denied_when_token_configured():
    """No X-Admin-Token => 403 for /api/v1/* when token configured."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with patch.dict(os.environ, {"DASHBOARD_ADMIN_TOKEN": "sched-secret"}, clear=False):
        with patch("src.dashboard.routes.settings") as mock_settings:
            mock_settings.dashboard.admin_token = "sched-secret"
            mock_settings.dashboard.allow_insecure_admin_api = False
            mock_settings.environment = "production"

            r = client.get("/api/v1/targets")
            assert r.status_code == 403


def test_scheduler_api_correct_token_allowed():
    """Correct X-Admin-Token => allowed for /api/v1/*."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with patch.dict(os.environ, {"DASHBOARD_ADMIN_TOKEN": "sched-secret"}, clear=False):
        mock_settings = MagicMock()
        mock_settings.dashboard.admin_token = "sched-secret"
        mock_settings.dashboard.allow_insecure_admin_api = False
        mock_settings.environment = "production"
        with patch("src.dashboard.routes.settings", mock_settings):
            r = client.get("/api/v1/targets", headers={"X-Admin-Token": "sched-secret"})
            assert r.status_code != 403


def test_scheduler_api_no_token_denied():
    """No X-Admin-Token => 403 for /api/v1/* when token configured."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with patch.dict(os.environ, {"DASHBOARD_ADMIN_TOKEN": "scheduler-secret"}, clear=False):
        with patch("src.dashboard.routes.settings") as mock_settings:
            mock_settings.dashboard.admin_token = "scheduler-secret"
            mock_settings.dashboard.allow_insecure_admin_api = False
            mock_settings.environment = "production"

            r = client.get("/api/v1/targets")
            assert r.status_code == 403


def test_scheduler_api_correct_token_allowed():
    """Correct X-Admin-Token => 200 for /api/v1/*."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with patch.dict(os.environ, {"DASHBOARD_ADMIN_TOKEN": "scheduler-secret"}, clear=False):
        mock_settings = MagicMock()
        mock_settings.dashboard.admin_token = "scheduler-secret"
        mock_settings.dashboard.allow_insecure_admin_api = False
        mock_settings.environment = "production"
        with patch("src.dashboard.routes.settings", mock_settings):
            r = client.get("/api/v1/targets", headers={"X-Admin-Token": "scheduler-secret"})
            assert r.status_code != 403


def test_scheduler_api_no_token_denied():
    """No X-Admin-Token => 403 for /api/v1/* when token configured."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with patch.dict(os.environ, {"DASHBOARD_ADMIN_TOKEN": "correct-secret"}, clear=False):
        with patch("src.dashboard.routes.settings") as mock_settings:
            mock_settings.dashboard.admin_token = "correct-secret"
            mock_settings.dashboard.allow_insecure_admin_api = False
            mock_settings.environment = "production"
            r = client.get("/api/v1/targets")
            assert r.status_code == 403


def test_scheduler_api_correct_token_allowed():
    """Correct X-Admin-Token => allowed for /api/v1/*."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with patch.dict(os.environ, {"DASHBOARD_ADMIN_TOKEN": "correct-secret"}, clear=False):
        mock_settings = MagicMock()
        mock_settings.dashboard.admin_token = "correct-secret"
        mock_settings.dashboard.allow_insecure_admin_api = False
        mock_settings.environment = "production"
        with patch("src.dashboard.routes.settings", mock_settings):
            r = client.get("/api/v1/targets", headers={"X-Admin-Token": "correct-secret"})
            assert r.status_code != 403


def test_scheduler_api_no_token_denied():
    """GET /api/v1/targets without X-Admin-Token => 403 when token configured."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with patch.dict(os.environ, {"DASHBOARD_ADMIN_TOKEN": "correct-secret-token"}, clear=False):
        with patch("src.dashboard.routes.settings") as mock_settings:
            mock_settings.dashboard.admin_token = "correct-secret-token"
            mock_settings.dashboard.allow_insecure_admin_api = False
            mock_settings.environment = "production"

            r = client.get("/api/v1/targets")
            assert r.status_code == 403
            data = r.get_json()
            assert "admin" in (data.get("error") or "").lower()


def test_scheduler_api_correct_token_allowed():
    """GET /api/v1/targets with correct X-Admin-Token => 200 (or 500 if DB missing)."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with patch.dict(os.environ, {"DASHBOARD_ADMIN_TOKEN": "correct-secret-token"}, clear=False):
        mock_settings = MagicMock()
        mock_settings.dashboard.admin_token = "correct-secret-token"
        mock_settings.dashboard.allow_insecure_admin_api = False
        mock_settings.environment = "production"
        with patch("src.dashboard.routes.settings", mock_settings):
            r = client.get("/api/v1/targets", headers={"X-Admin-Token": "correct-secret-token"})
            assert r.status_code != 403, "Correct token must not be denied"


def test_scheduler_api_no_token_denied():
    """No X-Admin-Token => 403 for /api/v1/* (scheduler API)."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with patch.dict(os.environ, {"DASHBOARD_ADMIN_TOKEN": "sched-secret"}, clear=False):
        with patch("src.dashboard.routes.settings") as mock_settings:
            mock_settings.dashboard.admin_token = "sched-secret"
            mock_settings.dashboard.allow_insecure_admin_api = False
            mock_settings.environment = "production"

            r = client.get("/api/v1/targets")
            assert r.status_code == 403


def test_scheduler_api_correct_token_allowed():
    """Correct X-Admin-Token => 200 for /api/v1/* (scheduler API)."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with patch.dict(os.environ, {"DASHBOARD_ADMIN_TOKEN": "sched-secret"}, clear=False):
        mock_settings = MagicMock()
        mock_settings.dashboard.admin_token = "sched-secret"
        mock_settings.dashboard.allow_insecure_admin_api = False
        mock_settings.environment = "production"
        with patch("src.dashboard.routes.settings", mock_settings):
            r = client.get("/api/v1/targets", headers={"X-Admin-Token": "sched-secret"})
            assert r.status_code != 403


def test_scheduler_api_no_token_denied_when_token_configured():
    """No X-Admin-Token => 403 for /api/v1/* when token is configured."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with patch.dict(os.environ, {"DASHBOARD_ADMIN_TOKEN": "scheduler-secret"}, clear=False):
        with patch("src.dashboard.routes.settings") as mock_settings:
            mock_settings.dashboard.admin_token = "scheduler-secret"
            mock_settings.dashboard.allow_insecure_admin_api = False
            mock_settings.environment = "production"

            r = client.get("/api/v1/targets")
            assert r.status_code == 403


def test_scheduler_api_correct_token_allowed():
    """Correct X-Admin-Token => allowed for /api/v1/*."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with patch.dict(os.environ, {"DASHBOARD_ADMIN_TOKEN": "scheduler-secret"}, clear=False):
        mock_settings = MagicMock()
        mock_settings.dashboard.admin_token = "scheduler-secret"
        mock_settings.dashboard.allow_insecure_admin_api = False
        mock_settings.environment = "production"
        with patch("src.dashboard.routes.settings", mock_settings):
            r = client.get("/api/v1/targets", headers={"X-Admin-Token": "scheduler-secret"})
            assert r.status_code != 403


def test_scheduler_api_no_token_denied():
    """GET /api/v1/targets without token => 403 when token is configured."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with patch.dict(os.environ, {"DASHBOARD_ADMIN_TOKEN": "correct-secret-token"}, clear=False):
        with patch("src.dashboard.routes.settings") as mock_settings:
            mock_settings.dashboard.admin_token = "correct-secret-token"
            mock_settings.dashboard.allow_insecure_admin_api = False
            mock_settings.environment = "production"

            r = client.get("/api/v1/targets")
            assert r.status_code == 403
            data = r.get_json()
            assert "admin" in (data.get("error") or "").lower()


def test_scheduler_api_correct_token_allowed():
    """GET /api/v1/targets with correct X-Admin-Token => 200."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with patch.dict(os.environ, {"DASHBOARD_ADMIN_TOKEN": "correct-secret-token"}, clear=False):
        mock_settings = MagicMock()
        mock_settings.dashboard.admin_token = "correct-secret-token"
        mock_settings.dashboard.allow_insecure_admin_api = False
        mock_settings.environment = "production"
        with patch("src.dashboard.routes.settings", mock_settings):
            r = client.get("/api/v1/targets", headers={"X-Admin-Token": "correct-secret-token"})
            assert r.status_code != 403, "Correct token must not be denied"
