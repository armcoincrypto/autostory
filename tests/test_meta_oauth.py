"""Meta OAuth, encryption, discovery, and publishing-block tests (no live Meta calls)."""
from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.database import Base
import src.social_agent.models  # noqa: F401
from src.social_agent.credential_crypto import SocialCredentialCrypto
from src.social_agent.meta_connection import (
    assert_meta_publishing_disabled,
    disconnect_meta,
    get_meta_connection,
    handle_meta_callback,
    meta_health_check,
    select_meta_destination,
    start_meta_oauth,
)
from src.social_agent.models import SocialConnection, SocialOAuthState
from src.social_agent.providers.meta import MetaProviderAdapter, GRAPH_API_VERSION, READ_ONLY_SCOPES
from src.social_agent.tools import get_tool


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


def test_graph_api_version_pinned():
    assert GRAPH_API_VERSION == "v25.0"
    assert "pages_show_list" in READ_ONLY_SCOPES
    assert "instagram_basic" in READ_ONLY_SCOPES
    assert "instagram_content_publish" not in READ_ONLY_SCOPES


def test_credential_encrypt_roundtrip(crypto_env):
    crypto = SocialCredentialCrypto.from_environment()
    env = crypto.encrypt_json({"user_access_token": "tok-abc", "page_access_tokens": {"1": "pt"}})
    assert env.startswith("enc:v1:aes256gcm:")
    assert "tok-abc" not in env
    out = crypto.decrypt_json(env)
    assert out["user_access_token"] == "tok-abc"


def test_missing_meta_credentials(monkeypatch, db_session):
    monkeypatch.delenv("META_APP_ID", raising=False)
    monkeypatch.delenv("META_APP_SECRET", raising=False)
    monkeypatch.delenv("META_REDIRECT_URI", raising=False)
    out = start_meta_oauth(db_session, actor="admin")
    assert out["ok"] is False
    assert out["error"] == "CREDENTIALS_MISSING"


def test_http_redirect_rejected_in_production(monkeypatch, db_session, crypto_env):
    monkeypatch.setenv("META_REDIRECT_URI", "http://ex.zellotex.com/social-agent/accounts/meta/callback")
    monkeypatch.setenv("ENVIRONMENT", "production")
    # config.configured becomes false because redirect_https required
    out = start_meta_oauth(db_session, actor="admin")
    assert out["ok"] is False


def test_oauth_state_validation(crypto_env, db_session):
    adapter = MetaProviderAdapter(http=lambda *a, **k: (500, {}))
    started = start_meta_oauth(db_session, actor="admin", adapter=adapter)
    assert started["ok"]
    state = db_session.query(SocialOAuthState).one().state

    # wrong user
    bad = handle_meta_callback(db_session, actor="other", state=state, code="x", adapter=adapter)
    assert bad["error"] == "wrong_user"

    # fresh state for reuse/expiry tests
    started2 = start_meta_oauth(db_session, actor="admin", adapter=adapter)
    row = db_session.query(SocialOAuthState).filter(SocialOAuthState.state != state).order_by(SocialOAuthState.id.desc()).first()
    row.expires_at = datetime.utcnow() - timedelta(seconds=5)
    expired = handle_meta_callback(db_session, actor="admin", state=row.state, code="x", adapter=adapter)
    assert expired["error"] == "expired_state"

    started3 = start_meta_oauth(db_session, actor="admin", adapter=adapter)
    st = db_session.query(SocialOAuthState).order_by(SocialOAuthState.id.desc()).first()
    st.used_at = datetime.utcnow()
    reused = handle_meta_callback(db_session, actor="admin", state=st.state, code="x", adapter=adapter)
    assert reused["error"] == "reused_state"

    missing = handle_meta_callback(db_session, actor="admin", state=None, code="x", adapter=adapter)
    assert missing["error"] == "missing_state"


class _FakeMetaHttp:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, headers=None, body=None, timeout=20.0):
        self.calls.append(url)
        if "oauth/access_token" in url and "fb_exchange_token" in url:
            return 200, {"access_token": "LONG_TOKEN", "token_type": "bearer", "expires_in": 5184000}
        if "oauth/access_token" in url:
            return 200, {"access_token": "SHORT_TOKEN", "token_type": "bearer", "expires_in": 3600}
        if "debug_token" in url:
            return 200, {"data": {"is_valid": True, "scopes": list(READ_ONLY_SCOPES), "user_id": "99"}}
        if url.startswith("https://graph.facebook.com/") and "/me?" in url and "accounts" not in url:
            return 200, {"id": "99", "name": "Tester"}
        if "/me/accounts" in url:
            return 200, {
                "data": [
                    {
                        "id": "page-1",
                        "name": "Exswaping Page",
                        "category": "Business",
                        "tasks": ["MANAGE", "CREATE_CONTENT"],
                        "access_token": "PAGE_TOKEN_1",
                        "instagram_business_account": {"id": "ig-1", "username": "exswaping"},
                    },
                    {
                        "id": "page-2",
                        "name": "Other Page",
                        "access_token": "PAGE_TOKEN_2",
                    },
                ]
            }
        if "/page-1?" in url or url.rstrip("/").endswith("/page-1"):
            return 200, {"id": "page-1", "name": "Exswaping Page", "instagram_business_account": {"id": "ig-1", "username": "exswaping"}}
        if "/page-2?" in url:
            return 200, {"id": "page-2", "name": "Other Page"}
        return 500, {"error": {"message": "unexpected", "code": 1}}


def test_callback_discovers_pages_and_encrypts(crypto_env, db_session):
    http = _FakeMetaHttp()
    adapter = MetaProviderAdapter(http=http)
    started = start_meta_oauth(db_session, actor="admin", adapter=adapter)
    assert started["ok"]
    assert "authorization_url" in started
    assert "access_token" not in started["authorization_url"]
    state = db_session.query(SocialOAuthState).order_by(SocialOAuthState.id.desc()).first().state
    out = handle_meta_callback(db_session, actor="admin", state=state, code="AUTH_CODE", adapter=adapter)
    assert out["ok"]
    assert out["pages_discovered"] == 2
    assert out["instagram_accounts_discovered"] == 1
    assert out["needs_page_selection"] is True
    assert out["provider_mutations"] == 0
    conn = db_session.query(SocialConnection).one()
    assert conn.credentials_encrypted.startswith("enc:v1:aes256gcm:")
    assert "SHORT_TOKEN" not in (conn.credentials_encrypted or "")
    assert "LONG_TOKEN" not in (conn.credentials_encrypted or "")
    assert "PAGE_TOKEN" not in (conn.credentials_encrypted or "")
    pub = get_meta_connection(db_session)["connection"]
    blob = json.dumps(pub)
    assert "LONG_TOKEN" not in blob
    assert "PAGE_TOKEN" not in blob
    assert "access_token" not in blob


def test_select_and_health(crypto_env, db_session):
    http = _FakeMetaHttp()
    adapter = MetaProviderAdapter(http=http)
    start_meta_oauth(db_session, actor="admin", adapter=adapter)
    state = db_session.query(SocialOAuthState).order_by(SocialOAuthState.id.desc()).first().state
    handle_meta_callback(db_session, actor="admin", state=state, code="AUTH_CODE", adapter=adapter)
    conn = db_session.query(SocialConnection).one()
    selected = select_meta_destination(
        db_session, actor="admin", connection_id=conn.id, page_id="page-1", instagram_account_id="ig-1"
    )
    assert selected["ok"]
    assert selected["connection"]["selected_page_id"] == "page-1"
    health = meta_health_check(db_session, actor="admin", connection_id=conn.id, adapter=adapter)
    assert health["ok"]
    assert health["health"] == "CONNECTED"
    assert health["provider_mutations"] == 0


def test_disconnect_requires_confirm(crypto_env, db_session):
    http = _FakeMetaHttp()
    adapter = MetaProviderAdapter(http=http)
    start_meta_oauth(db_session, actor="admin", adapter=adapter)
    state = db_session.query(SocialOAuthState).order_by(SocialOAuthState.id.desc()).first().state
    handle_meta_callback(db_session, actor="admin", state=state, code="c", adapter=adapter)
    conn = db_session.query(SocialConnection).one()
    denied = disconnect_meta(db_session, actor="admin", connection_id=conn.id, confirm=False)
    assert denied["error"] == "confirmation_required"
    ok = disconnect_meta(db_session, actor="admin", connection_id=conn.id, confirm=True)
    assert ok["ok"]
    assert ok["connection"]["status"] == "disconnected"
    assert db_session.query(SocialConnection).one().credentials_encrypted is None


def test_publishing_blocked():
    blocked = assert_meta_publishing_disabled()
    assert blocked["ok"] is False
    assert blocked["error"] in {"UNSUPPORTED", "NOT_AUTHORIZED"}
    pub = get_tool("publishing.publish")
    assert pub is not None
    assert pub.available is False


def test_single_page_auto_select(crypto_env, db_session):
    class OnePage(_FakeMetaHttp):
        def __call__(self, method, url, headers=None, body=None, timeout=20.0):
            if "/me/accounts" in url:
                return 200, {
                    "data": [
                        {
                            "id": "page-1",
                            "name": "Only",
                            "access_token": "PAGE_TOKEN_1",
                            "instagram_business_account": {"id": "ig-1", "username": "only"},
                        }
                    ]
                }
            return super().__call__(method, url, headers, body, timeout)

    adapter = MetaProviderAdapter(http=OnePage())
    start_meta_oauth(db_session, actor="admin", adapter=adapter)
    state = db_session.query(SocialOAuthState).order_by(SocialOAuthState.id.desc()).first().state
    out = handle_meta_callback(db_session, actor="admin", state=state, code="c", adapter=adapter)
    assert out["ok"]
    assert out["needs_page_selection"] is False
    assert out["connection"]["selected_page_id"] == "page-1"


def test_api_routes_meta(crypto_env, monkeypatch):
    monkeypatch.setenv("DASHBOARD_ADMIN_TOKEN", "meta-test-token")
    from src.dashboard.app import create_app

    app = create_app()
    client = app.test_client()
    headers = {"X-Admin-Token": "meta-test-token"}
    res = client.get("/api/v1/social-agent/connections/meta", headers=headers)
    assert res.status_code == 200
    body = res.get_json()
    assert body["ok"] is True
    assert "connection" in body

    res2 = client.post("/api/v1/social-agent/connections/meta/publish", headers=headers, json={})
    assert res2.status_code == 403

    # unauthenticated
    assert client.get("/api/v1/social-agent/connections/meta").status_code == 401
