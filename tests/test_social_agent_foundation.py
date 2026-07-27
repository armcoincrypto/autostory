"""Social Agent foundation tests — registry, tools, services, routes."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.database import Base
from src.social_agent.permissions import ALL_PERMISSIONS, actor_permissions, require_permission
from src.social_agent.registry import get_agent, list_platform_agents
from src.social_agent.tools import get_tool, list_tools
from src.social_agent import services
from src.social_agent.integrations import integration_matrix, meta_status
import src.social_agent.models  # noqa: F401 — register metadata


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
        session.commit()
    finally:
        session.close()


def test_social_agent_registered_in_platform_registry():
    agents = list_platform_agents()
    ids = {a["agent_id"] for a in agents}
    assert "social_agent" in ids
    assert "ai_agent" in ids
    assert "ai_coding" in ids
    assert "broadcast" in ids
    sa = get_agent("social_agent")
    assert sa is not None
    assert sa["route"] == "/social-agent"
    assert sa["status"] == "active"


def test_tool_registry_marks_live_publish_unavailable():
    pub = get_tool("publishing.publish")
    assert pub is not None
    assert pub.available is False
    assert pub.confirmation_required is True
    tools = {t["name"]: t for t in list_tools()}
    assert "publishing.publish" in tools
    assert "exswaping.get_public_content" not in tools


def test_permissions_admin_has_full_set():
    perms = actor_permissions(is_dashboard_admin=True)
    assert "social_agent.view" in perms
    assert "publishing.publish" in perms
    ok, err = require_permission(perms, "content.create")
    assert ok and err is None
    ok, err = require_permission(set(), "content.create")
    assert not ok and err


def test_exswaping_integration_removed():
    m = integration_matrix()
    assert "exswaping" not in m
    assert get_tool("exswaping.get_public_content") is None


def test_meta_credentials_missing_by_default(monkeypatch):
    monkeypatch.delenv("META_APP_ID", raising=False)
    monkeypatch.delenv("META_APP_SECRET", raising=False)
    monkeypatch.delenv("META_REDIRECT_URI", raising=False)
    st = meta_status()
    assert st["configured"] is False
    assert st["status"] == "CREDENTIALS_MISSING"


def test_generate_variants_local_respects_x_limit():
    long_brief = "A" * 400
    out = services.generate_variants_local(long_brief, ["x", "telegram"], ["EN"])
    assert out["ok"]
    x_var = next(v for v in out["variants"] if v["platform"] == "x")
    assert x_var["char_count"] <= 280
    assert x_var["validation"]["ok"] is True


def test_create_draft_and_preview(db_session):
    created = services.create_draft(
        db_session,
        actor="test",
        title="USDT AMD",
        brief="New USDT to AMD direction",
        body="New USDT to AMD direction",
        platforms=["telegram", "facebook"],
        languages=["EN"],
    )
    assert created["ok"]
    assert created["content_id"]
    assert len(created["variants"]) == 2
    preview = services.publish_preview(
        db_session,
        actor="test",
        content_id=created["content_id"],
        destinations=["telegram", "facebook"],
    )
    assert preview["ok"] is True
    assert preview["dry_run"] is True
    assert preview["published"] is False
    assert preview["provider_called"] is False
    assert preview["meta_provider_mutations"] == 0
    assert any(d.get("destination") == "telegram" for d in preview["destinations"])
    # Facebook maps into Meta dry-run payload path.
    assert preview.get("dry_run_id") or any(
        d.get("destination") in {"facebook", "facebook_page"} or d.get("payload_preview")
        for d in preview["destinations"]
    )


def test_execute_tool_blocks_live_publish(db_session):
    out = services.execute_tool(
        db_session,
        actor="test",
        perms=set(ALL_PERMISSIONS),
        tool_name="publishing.publish",
        arguments={"content_id": 1},
        dry_run=False,
        confirmation_token="CONFIRM",
    )
    assert out["ok"] is False
    assert out["error"] in {"UNSUPPORTED", "NOT_CONFIGURED"}


def test_chat_turn_creates_draft(db_session):
    out = services.chat_turn(
        db_session,
        actor="test",
        perms=set(ALL_PERMISSIONS),
        conversation_id=None,
        message="Create a Telegram post about USDT to AMD",
    )
    assert out["ok"]
    assert out["conversation_id"]
    assert out["tool_calls"]
    assert out["tool_calls"][0]["tool"] == "content.generate_variants"
    assert out["ai_provider_used"] is False


def test_integration_matrix_keys():
    m = integration_matrix()
    for key in ("meta", "telegram", "x", "linkedin", "discord", "youtube", "tiktok", "ai"):
        assert key in m
    assert "exswaping" not in m


def test_social_agent_routes_registered():
    from src.dashboard.app import create_app

    app = create_app()
    paths = {r.rule for r in app.url_map.iter_rules()}
    assert "/social-agent" in paths
    assert "/agents" in paths
    assert "/api/v1/social-agent/health" in paths
    assert "/api/v1/social-agent/overview" in paths
    assert "/api/v1/social-agent/chat" in paths
    assert "/api/v1/social-agent/content" in paths
    assert "/api/v1/social-agent/meta/oauth/start" in paths
    assert "/api/v1/social-agent/publishing/dry-run" in paths
    assert "/api/v1/social-agent/publishing/history" in paths
    assert "/api/v1/social-agent/publishing/preview/<int:dry_run_id>" in paths


def test_social_agent_api_requires_auth():
    from src.dashboard.app import create_app

    app = create_app()
    client = app.test_client()
    for path in (
        "/api/v1/social-agent/health",
        "/api/v1/social-agent/overview",
        "/api/v1/social-agent/agents",
    ):
        res = client.get(path)
        assert res.status_code == 401
        assert res.get_json().get("error") == "unauthorized"


def test_agents_page_redirects_when_unauthenticated():
    from src.dashboard.app import create_app

    app = create_app()
    client = app.test_client()
    res = client.get("/agents", follow_redirects=False)
    assert res.status_code in {302, 401}
    if res.status_code == 302:
        assert "/login" in (res.headers.get("Location") or "")


def test_social_agent_registry_has_single_social_entry():
    agents = list_platform_agents()
    social = [a for a in agents if a["agent_id"] == "social_agent"]
    assert len(social) == 1


def test_debug_endpoint_disabled_by_default(monkeypatch):
    from src.dashboard.app import create_app

    monkeypatch.delenv("SOCIAL_AGENT_DEBUG_ENABLED", raising=False)
    monkeypatch.setenv("DASHBOARD_ADMIN_TOKEN", "test-social-agent-token")
    app = create_app()
    client = app.test_client()
    res = client.get(
        "/api/v1/social-agent/debug",
        headers={"X-Admin-Token": "test-social-agent-token"},
    )
    assert res.status_code == 404


def test_meta_oauth_start_credentials_missing(monkeypatch):
    from src.dashboard.app import create_app

    monkeypatch.delenv("META_APP_ID", raising=False)
    monkeypatch.delenv("META_APP_SECRET", raising=False)
    monkeypatch.delenv("META_REDIRECT_URI", raising=False)
    monkeypatch.setenv("DASHBOARD_ADMIN_TOKEN", "test-social-agent-token")
    app = create_app()
    client = app.test_client()
    res = client.get(
        "/api/v1/social-agent/meta/oauth/start",
        headers={"X-Admin-Token": "test-social-agent-token"},
    )
    assert res.status_code == 503
    body = res.get_json()
    assert body["error"] == "CREDENTIALS_MISSING"
    assert body["meta"]["configured"] is False
    # Never return secret material; env var names in operator docs are allowed only if absent here.
    blob = str(body).lower()
    assert "app_secret" not in blob
    assert "access_token" not in blob
    assert "bearer" not in blob
