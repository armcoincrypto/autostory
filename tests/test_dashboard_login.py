"""Tests for dashboard admin login flow and web route protection."""
import os
from unittest.mock import patch

import pytest


def test_login_page_returns_200():
    """GET /login returns 200 and shows form."""
    from src.dashboard.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    r = client.get("/login")
    assert r.status_code == 200
    assert b"username" in r.data.lower() or b"login" in r.data.lower()


def test_unauthenticated_redirects_to_login():
    """GET / without session redirects to /login."""
    from src.dashboard.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in (r.location or "")


def test_login_and_dashboard_access():
    """Create admin, log in, access dashboard and API with session."""
    from src.dashboard.app import create_app
    from src.core.database import init_db, get_db_context
    from src.dashboard.models import DashboardUser

    init_db()
    with get_db_context() as db:
        user = db.query(DashboardUser).filter(DashboardUser.username == "testadmin").first()
        if not user:
            user = DashboardUser(username="testadmin", email="testadmin@test", is_admin=True, is_active=True)
            user.set_password("testpass123")
            db.add(user)
            db.commit()

    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False  # Disable CSRF for test client
    client = app.test_client()

    r = client.post("/login", data={"username": "testadmin", "password": "testpass123", "next": "/"}, follow_redirects=True)
    assert r.status_code == 200
    # With session, / should work
    r = client.get("/")
    assert r.status_code == 200


def test_logout_redirects_to_login():
    """GET /logout redirects to /login."""
    from src.dashboard.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    r = client.get("/logout", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in (r.location or "")


def test_unauthenticated_accounts_redirects_to_login():
    """GET /accounts without session redirects to /login."""
    from src.dashboard.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    r = client.get("/accounts", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in (r.location or "")


def test_authenticated_accounts_renders_main_page():
    """P10.13: GET /accounts renders operator accounts main page; diagnostics remain at /accounts-v2."""
    from src.dashboard.app import create_app
    from src.core.database import init_db, get_db_context
    from src.dashboard.models import DashboardUser

    init_db()
    with get_db_context() as db:
        user = db.query(DashboardUser).filter(DashboardUser.username == "testadmin").first()
        if not user:
            user = DashboardUser(username="testadmin", email="testadmin@test", is_admin=True, is_active=True)
            user.set_password("testpass123")
            db.add(user)
            db.commit()

    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    client = app.test_client()
    client.post("/login", data={"username": "testadmin", "password": "testpass123", "next": "/accounts"}, follow_redirects=True)
    r = client.get("/accounts", follow_redirects=False)
    assert r.status_code == 200
    assert b"Accounts" in r.data or b"account" in r.data.lower()
    r2 = client.get("/accounts-v2", follow_redirects=False)
    assert r2.status_code in (200, 404)


def _logged_in_client():
    """Helper: create app and logged-in test client."""
    from src.dashboard.app import create_app
    from src.core.database import init_db, get_db_context
    from src.dashboard.models import DashboardUser

    init_db()
    with get_db_context() as db:
        user = db.query(DashboardUser).filter(DashboardUser.username == "testadmin").first()
        if not user:
            user = DashboardUser(username="testadmin", email="testadmin@test", is_admin=True, is_active=True)
            user.set_password("testpass123")
            db.add(user)
            db.commit()

    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    client = app.test_client()
    client.post("/login", data={"username": "testadmin", "password": "testpass123"}, follow_redirects=True)
    return client


def test_unauthenticated_stories_redirects_to_login():
    """GET /stories without session redirects to /login."""
    from src.dashboard.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    r = client.get("/stories", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in (r.location or "")


def test_authenticated_stories_returns_200():
    """Logged-in admin can access /stories."""
    client = _logged_in_client()
    r = client.get("/stories")
    assert r.status_code == 200
    assert b"/api/stories" in r.data


def test_unauthenticated_discovery_redirects_to_login():
    """GET /discovery without session redirects to /login."""
    from src.dashboard.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    r = client.get("/discovery", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in (r.location or "")


def test_authenticated_discovery_returns_200():
    """Logged-in admin can access /discovery."""
    client = _logged_in_client()
    r = client.get("/discovery")
    assert r.status_code == 200
    assert b"/api/discovery" in r.data


def test_unauthenticated_campaigns_redirects_to_login():
    """GET /campaigns without session redirects to /login."""
    from src.dashboard.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    r = client.get("/campaigns", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in (r.location or "")


def test_authenticated_campaigns_returns_200():
    """Logged-in admin can access /campaigns."""
    client = _logged_in_client()
    r = client.get("/campaigns")
    assert r.status_code == 200
    assert b"/api/campaigns" in r.data


def test_unauthenticated_scheduler_redirects_to_login():
    """GET /scheduler without session redirects to /login."""
    from src.dashboard.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    r = client.get("/scheduler", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in (r.location or "")


def test_authenticated_scheduler_returns_200():
    """Logged-in admin can access /scheduler."""
    client = _logged_in_client()
    r = client.get("/scheduler")
    assert r.status_code == 200
    assert b"/api/v1" in r.data or b"api/v1" in r.data


def test_unauthenticated_dexpert_redirects_to_login():
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    r = client.get("/dexpert", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in (r.location or "")


def test_authenticated_dexpert_audit_returns_200():
    """Kathleen package may be BLOCKED_OWNER_SOURCE_REQUIRED — accept full audit or safe fallback."""
    client = _logged_in_client()
    r = client.get("/dexpert")
    assert r.status_code == 200
    body = r.data
    full_audit = (
        b"Dexpert audit" in body
        and b"Recent Dexpert conversations" in body
        and b"Kathleen plans" in body
    )
    safe_fallback = (
        b"Kathleen bridge" in body
        or b"waiting on the Kathleen" in body
        or b"Dexpert Runtime Audit" in body
    )
    assert full_audit or safe_fallback, "expected full Dexpert audit markers or Kathleen fallback HTML"


def _csrf_enabled_client():
    from src.dashboard.app import create_app

    app = create_app()
    app.config.update(
        TESTING=True,
        SESSION_COOKIE_SECURE=False,
        WTF_CSRF_ENABLED=True,
    )
    return app.test_client()


def _csrf_token_from_login_page(client, *, environ_overrides: dict | None = None) -> str:
    import re

    r = client.get("/login", environ_overrides=environ_overrides or {})
    assert r.status_code == 200
    m = re.search(rb'name="csrf_token" value="([^"]+)"', r.data)
    assert m is not None
    return m.group(1).decode()


def _production_login_post_env(host: str = "localhost") -> dict[str, str]:
    return {
        "HTTP_X_FORWARDED_PROTO": "https",
        "HTTP_X_FORWARDED_HOST": host,
        "HTTP_HOST": host,
        "HTTP_REFERER": f"https://{host}/login",
    }


def test_login_csrf_token_issued_on_get():
    client = _csrf_enabled_client()
    token = _csrf_token_from_login_page(client)
    assert len(token) > 20


def test_login_post_without_csrf_rejected():
    client = _csrf_enabled_client()
    _csrf_token_from_login_page(client)
    r = client.post(
        "/login",
        data={"username": "testadmin", "password": "wrong", "next": "/"},
        follow_redirects=False,
    )
    assert r.status_code == 400
    # The app re-renders a friendly login page on CSRF failure (with a fresh
    # token for the next attempt) rather than Flask-WTF's raw default text --
    # see the alert message in src/dashboard/templates/login.html /
    # the CSRF error handler in src/dashboard/app.py. The 400 status is the
    # actual security assertion; this checks the current, correct wording.
    assert b"Session expired or blocked" in r.data


def test_login_post_with_csrf_and_session_succeeds_or_shows_auth_error():
    from src.dashboard.app import create_app
    from src.core.database import init_db, get_db_context
    from src.dashboard.models import DashboardUser

    init_db()
    with get_db_context() as db:
        user = db.query(DashboardUser).filter(DashboardUser.username == "testadmin").first()
        if not user:
            user = DashboardUser(username="testadmin", email="testadmin@test", is_admin=True, is_active=True)
            user.set_password("testpass123")
            db.add(user)
            db.commit()

    app = create_app()
    app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False, WTF_CSRF_ENABLED=True)
    client = app.test_client()
    post_env = _production_login_post_env()
    csrf = _csrf_token_from_login_page(client, environ_overrides=post_env)
    r = client.post(
        "/login",
        data={"username": "testadmin", "password": "testpass123", "next": "/", "csrf_token": csrf},
        environ_overrides=post_env,
        follow_redirects=False,
    )
    assert r.status_code in (302, 200)
    if r.status_code == 302:
        assert r.location.endswith("/") or r.location == "/"
    r2 = client.get("/")
    assert r2.status_code == 200


def test_login_csrf_ssl_strict_disabled_missing_referrer_behind_https_proxy_still_accepted():
    """WTF_CSRF_SSL_STRICT is deliberately False (src/dashboard/app.py,
    _configure_reverse_proxy_and_session): Cloudflare and privacy browsers
    often omit Referer on POST, which would otherwise falsely fail CSRF for a
    legitimate session/token pair. A valid token behind an HTTPS proxy with no
    Referer must still be accepted -- this was previously asserted the other
    way (expecting a 400), testing a stricter posture the team explicitly
    moved away from for that reason.
    """
    client = _csrf_enabled_client()
    https_env = {
        "HTTP_X_FORWARDED_PROTO": "https",
        "HTTP_X_FORWARDED_HOST": "ex.zellotex.com",
        "HTTP_HOST": "ex.zellotex.com",
    }
    csrf = _csrf_token_from_login_page(client, environ_overrides=https_env)
    r = client.post(
        "/login",
        data={"username": "x", "password": "y", "next": "/", "csrf_token": csrf},
        environ_overrides=https_env,
        follow_redirects=False,
    )
    # No Referer header was sent above; a valid CSRF token must still be
    # accepted (not rejected as if it were a CSRF failure). The login itself
    # then fails on bad credentials, which the app renders as 200 (re-shown
    # login form) rather than a redirect.
    assert r.status_code == 200
    assert b"Session expired or blocked" not in r.data

    # With a matching Referer present, the outcome is the same (200) -- proving
    # Referer presence/absence no longer changes the CSRF outcome at all now
    # that SSL_STRICT is off, rather than just happening to pass once.
    csrf2 = _csrf_token_from_login_page(
        client,
        environ_overrides={**https_env, "HTTP_REFERER": "https://ex.zellotex.com/login"},
    )
    r_ok = client.post(
        "/login",
        data={"username": "x", "password": "y", "next": "/", "csrf_token": csrf2},
        environ_overrides={**https_env, "HTTP_REFERER": "https://ex.zellotex.com/login"},
        follow_redirects=False,
    )
    assert r_ok.status_code == 200
    assert b"Session expired or blocked" not in r_ok.data


def test_production_session_cookie_secure_flag_configured(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    from config.settings import settings

    monkeypatch.setattr(settings, "environment", "production")
    from src.dashboard.app import create_app

    app = create_app()
    assert app.config.get("SESSION_COOKIE_SECURE") is True
    assert app.config.get("SESSION_COOKIE_SAMESITE") == "Lax"
    assert app.config.get("SESSION_COOKIE_HTTPONLY") is True
