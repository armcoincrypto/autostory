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


def test_authenticated_accounts_returns_200_and_loads_template():
    """Logged-in admin can access /accounts; page contains loadAccounts / api/accounts dependency."""
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
    r = client.get("/accounts")
    assert r.status_code == 200
    # Template JS expects /api/accounts?summary=1
    assert b"/api/accounts" in r.data
    assert b"summary=1" in r.data


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
