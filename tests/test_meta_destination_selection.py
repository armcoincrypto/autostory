"""Regression: Meta destination selection persistence and display consistency."""
from __future__ import annotations

import json
import os
import base64

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.database import Base
import src.social_agent.models  # noqa: F401
from src.social_agent.meta_connection import (
    get_meta_connection,
    handle_meta_callback,
    public_connection_summary,
    select_meta_destination,
    start_meta_oauth,
)
from src.social_agent.models import SocialConnection, SocialOAuthState
from src.social_agent.providers.meta import MetaProviderAdapter
from src.social_agent.permissions import ALL_PERMISSIONS
from src.social_agent.services import execute_tool, overview
from tests.test_meta_oauth import _FakeMetaHttp


def _key_b64() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


@pytest.fixture
def crypto_env(monkeypatch):
    kid = "sa-cred-test"
    key = _key_b64()
    monkeypatch.setenv("SOCIAL_CREDENTIAL_ACTIVE_KEY_ID", kid)
    monkeypatch.setenv("SOCIAL_CREDENTIAL_KEYS_JSON", json.dumps({kid: key}))
    monkeypatch.setenv("META_APP_ID", "1234567890")
    monkeypatch.setenv("META_APP_SECRET", "test-secret-value")
    monkeypatch.setenv("META_REDIRECT_URI", "https://ex.zellotex.com/social-agent/accounts/meta/callback")
    monkeypatch.setenv("META_FACEBOOK_PUBLISHING_ENABLED", "false")
    monkeypatch.setenv("META_INSTAGRAM_PUBLISHING_ENABLED", "false")
    monkeypatch.setenv("ENVIRONMENT", "production")
    return kid, key


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


def _authorize(db_session, adapter):
    start_meta_oauth(db_session, actor="admin", adapter=adapter)
    state = db_session.query(SocialOAuthState).order_by(SocialOAuthState.id.desc()).first().state
    return handle_meta_callback(db_session, actor="admin", state=state, code="AUTH_CODE", adapter=adapter)


def test_no_auto_selection_and_exswaping_persist(crypto_env, db_session):
    adapter = MetaProviderAdapter(http=_FakeMetaHttp())
    out = _authorize(db_session, adapter)
    assert out["needs_page_selection"] is True
    assert out["connection"]["selected_page_id"] is None
    assert out["connection"]["display_name"] == "Meta (page selection required)"
    assert "Kobb" not in (out["connection"]["display_name"] or "")

    conn = db_session.query(SocialConnection).one()
    # Fake pages: page-1 = Exswaping, page-2 = Other
    selected = select_meta_destination(
        db_session, actor="admin", connection_id=conn.id, page_id="page-1", adapter=adapter
    )
    assert selected["ok"]
    assert selected["connection"]["status"] == "connected"
    assert selected["connection"]["selected_page_id"] == "page-1"
    assert selected["connection"]["selected_instagram_id"] == "ig-1"
    assert selected["connection"]["selected_page_name"] == "Exswaping Page"
    assert selected["connection"]["selected_instagram_username"] == "exswaping"
    assert selected["connection"]["display_name"] == "Exswaping Page"
    assert selected["connection"]["needs_page_selection"] is False

    # Reload preserves selection
    reloaded = get_meta_connection(db_session)
    assert reloaded["connection"]["selected_page_id"] == "page-1"
    assert reloaded["connection"]["status"] == "connected"
    assert reloaded["connection"]["display_name"] == "Exswaping Page"
    assert reloaded["needs_page_selection"] is False

    row = db_session.query(SocialConnection).one()
    assert row.status == "connected"
    assert row.selected_page_id == "page-1"
    assert row.selected_instagram_id == "ig-1"
    assert row.display_name == "Exswaping Page"


def test_change_page_updates_instagram_atomically(crypto_env, db_session):
    class TwoIg(_FakeMetaHttp):
        def __call__(self, method, url, headers=None, body=None, timeout=20.0):
            if "/page-2?" in url or url.rstrip("/").endswith("/page-2"):
                return 200, {
                    "id": "page-2",
                    "name": "Kobbex",
                    "category": "Currency Exchange",
                    "access_token": "PAGE_TOKEN_2",
                    "instagram_business_account": {
                        "id": "ig-2",
                        "username": "kobbexcrypto",
                        "name": "Kobbex",
                    },
                    "connected_instagram_account": {
                        "id": "ig-2",
                        "username": "kobbexcrypto",
                        "name": "Kobbex",
                    },
                }
            if "/me/accounts" in url:
                return 200, {
                    "data": [
                        {
                            "id": "page-1",
                            "name": "Exswaping Page",
                            "category": "Business",
                            "access_token": "PAGE_TOKEN_1",
                            "instagram_business_account": {
                                "id": "ig-1",
                                "username": "exswaping",
                                "name": "Exswaping",
                            },
                            "connected_instagram_account": {
                                "id": "ig-1",
                                "username": "exswaping",
                                "name": "Exswaping",
                            },
                        },
                        {
                            "id": "page-2",
                            "name": "Kobbex",
                            "category": "Currency Exchange",
                            "access_token": "PAGE_TOKEN_2",
                            "instagram_business_account": {
                                "id": "ig-2",
                                "username": "kobbexcrypto",
                                "name": "Kobbex",
                            },
                            "connected_instagram_account": {
                                "id": "ig-2",
                                "username": "kobbexcrypto",
                                "name": "Kobbex",
                            },
                        },
                    ]
                }
            return super().__call__(method, url, headers, body, timeout)

    adapter = MetaProviderAdapter(http=TwoIg())
    _authorize(db_session, adapter)
    conn = db_session.query(SocialConnection).one()
    select_meta_destination(db_session, actor="admin", connection_id=conn.id, page_id="page-1", adapter=adapter)
    changed = select_meta_destination(
        db_session, actor="admin", connection_id=conn.id, page_id="page-2", adapter=adapter
    )
    assert changed["ok"]
    assert changed["connection"]["selected_page_id"] == "page-2"
    assert changed["connection"]["selected_page_name"] == "Kobbex"
    assert changed["connection"]["selected_instagram_id"] == "ig-2"
    assert changed["connection"]["selected_instagram_username"] == "kobbexcrypto"
    assert changed["connection"]["display_name"] == "Kobbex"


def test_other_connections_never_shows_stale_meta_user(crypto_env, db_session):
    adapter = MetaProviderAdapter(http=_FakeMetaHttp())
    _authorize(db_session, adapter)
    conn = db_session.query(SocialConnection).one()
    # Simulate stale Meta-user display_name left from OAuth callback.
    conn.display_name = "Kobb Ex"
    db_session.flush()
    pending = public_connection_summary(conn)
    assert pending["display_name"] == "Meta (page selection required)"
    assert "Kobb Ex" not in pending["display_name"]

    select_meta_destination(db_session, actor="admin", connection_id=conn.id, page_id="page-1", adapter=adapter)
    listed = execute_tool(
        db_session,
        actor="admin",
        perms=set(ALL_PERMISSIONS),
        tool_name="social.list_connections",
        arguments={},
        dry_run=True,
    )
    names = [c["display_name"] for c in listed["connections"]]
    assert names == ["Exswaping Page"]
    assert "Kobb Ex" not in names

    ov = overview(db_session)
    assert ov["connections"][0]["display_name"] == "Exswaping Page"
    assert "Kobb Ex" not in json.dumps(ov["connections"])


def test_accounts_template_change_page_affordance():
    text = open("src/dashboard/templates/social_agent/accounts.html", encoding="utf-8").read()
    assert "Change Page" in text
    assert "Select a Facebook Page" in text
    assert "Never fall back to destinations[0]" in text or "fakes a selection" in text
    # Ensure the old silent first-page fallback is gone.
    assert "|| dests[0]" not in text
    assert "|| dests[0] ||" not in text
