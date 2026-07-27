"""Phase 2.1 UI polish — global search, shell chrome, fail-closed surfaces."""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.database import Base
import src.social_agent.models  # noqa: F401
from src.social_agent import services
from src.social_agent import brand_store as brand
from src.dashboard.app import create_app


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "src" / "dashboard" / "templates" / "social_agent"
STATIC = ROOT / "src" / "dashboard" / "static" / "social_agent"


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


def test_global_search_finds_draft_brand_and_nav(db_session):
    created = services.create_draft(
        db_session,
        actor="tester",
        title="USDT rates brief",
        brief="Clear Exswaping rates",
        body="Body",
        platforms=["facebook"],
        languages=["EN"],
    )
    brand.upsert_knowledge(
        db_session,
        actor="tester",
        category="tone",
        key="voice",
        title="Voice",
        value="Clear AML-aware tone",
    )
    out = services.global_search(db_session, query="usdt", limit=20)
    assert out["ok"] is True
    assert out["provider_called"] is False
    types = {r["type"] for r in out["results"]}
    assert "content" in types
    assert any(r["id"] == created["content_id"] for r in out["results"] if r["type"] == "content")

    brand_hits = services.global_search(db_session, query="aml", limit=10)
    assert any(r["type"] == "brand" for r in brand_hits["results"])

    nav = services.global_search(db_session, query="studio", limit=10)
    assert any(r["type"] == "page" and "content" in (r["href"] or "") for r in nav["results"])

    empty = services.global_search(db_session, query="", limit=10)
    assert empty["results"] == []


def test_search_api_route_registered():
    app = create_app()
    rules = {str(r) for r in app.url_map.iter_rules()}
    assert "/api/v1/social-agent/search" in rules


def test_search_api_requires_auth_and_returns_results(monkeypatch):
    monkeypatch.setenv("DASHBOARD_ADMIN_TOKEN", "test-social-agent-token")
    app = create_app()
    client = app.test_client()
    denied = client.get("/api/v1/social-agent/search?q=overview")
    assert denied.status_code in (401, 403)
    ok = client.get(
        "/api/v1/social-agent/search?q=overview&limit=5",
        headers={"X-Admin-Token": "test-social-agent-token"},
    )
    assert ok.status_code == 200
    body = ok.get_json()
    assert body["ok"] is True
    assert body["provider_called"] is False
    assert isinstance(body["results"], list)


def test_shell_has_command_palette_search_and_a11y():
    shell = (TEMPLATES / "shell.html").read_text(encoding="utf-8")
    assert 'id="sa-command-modal"' in shell
    assert 'id="sa-search-modal"' in shell
    assert 'class="sa-skip-link"' in shell
    assert 'id="sa-nav-toggle"' in shell
    assert 'id="sa-theme-toggle"' in shell
    assert 'aria-live="polite"' in shell

    js = (STATIC / "social_agent.js").read_text(encoding="utf-8")
    assert "loadingHtml" in js
    assert "emptyHtml" in js
    assert "errorHtml" in js
    assert "/search?q=" in js
    assert "Ctrl" in js or "metaKey" in js

    css = (STATIC / "social_agent.css").read_text(encoding="utf-8")
    assert "@media (max-width: 980px)" in css
    assert "sa-nav-open" in css
    assert "prefers-reduced-motion" in css


def test_pages_use_shared_ui_states():
    for name in (
        "overview.html",
        "content.html",
        "media.html",
        "calendar.html",
        "brand.html",
        "assistant.html",
        "analytics.html",
        "comments.html",
        "messages.html",
        "automations.html",
        "settings.html",
        "publishing.html",
    ):
        html = (TEMPLATES / name).read_text(encoding="utf-8")
        assert "SocialAgent.ui." in html or name == "content.html"
        if name == "content.html":
            studio = (STATIC / "content_studio.js").read_text(encoding="utf-8")
            assert "SocialAgent.ui.loadingHtml" in studio
            assert "SocialAgent.ui.emptyHtml" in studio
            assert "SocialAgent.ui.errorHtml" in studio


def test_fail_closed_copy_present_on_mutation_surfaces():
    content = (TEMPLATES / "content.html").read_text(encoding="utf-8")
    assert "never auto-publishes" in content.lower() or "never auto-publish" in content.lower()
    calendar = (TEMPLATES / "calendar.html").read_text(encoding="utf-8")
    assert "Live publishing disabled" in calendar or "live publish" in calendar.lower()
    autos = (TEMPLATES / "automations.html").read_text(encoding="utf-8")
    assert "disabled" in autos.lower()
    analytics = (TEMPLATES / "analytics.html").read_text(encoding="utf-8")
    assert "No fabricated" in analytics or "fabricated" in analytics.lower()
