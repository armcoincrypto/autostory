"""Facebook controlled-canary publishing — gates, auth object, single POST."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.database import Base
import src.social_agent.models  # noqa: F401
from src.social_agent.models import (
    SocialConnection,
    SocialIdempotencyRecord,
    SocialPublishCanaryAuthorization,
    SocialPublishReceipt,
)
from src.social_agent.permissions import ALL_PERMISSIONS
from src.social_agent import services
from src.social_agent.providers.meta import MetaProviderAdapter
from src.social_agent.publishing.canary_auth import CanaryAuthorization, mint_canary_authorization
from src.social_agent.publishing.execution import (
    CANARY_FACEBOOK_PAGE_ID,
    CANARY_MESSAGE,
    PublishingExecutionMode,
)
from src.social_agent.publishing import publish_service as canary
from src.social_agent.tools import get_tool


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


@pytest.fixture
def gates_disabled(monkeypatch):
    monkeypatch.setenv("META_FACEBOOK_PUBLISHING_ENABLED", "false")
    monkeypatch.setenv("META_INSTAGRAM_PUBLISHING_ENABLED", "false")
    monkeypatch.setenv("META_PUBLISHING_EXECUTION_MODE", "disabled")


def _seed_connection(db, *, page_id=CANARY_FACEBOOK_PAGE_ID):
    # Minimal encrypted-looking placeholder is not used when we monkeypatch decrypt.
    db.add(
        SocialConnection(
            provider="meta",
            workspace_id="default",
            display_name="Exswaping",
            status="connected",
            health="CONNECTED",
            selected_page_id=page_id,
            selected_instagram_id="17841478010207208",
            permissions_json=json.dumps(["pages_show_list", "pages_read_engagement", "pages_manage_posts"]),
            credentials_encrypted="enc:v1:aes256gcm:test:unused",
            destinations_json=json.dumps(
                [{"page_id": page_id, "page_name": "Exswaping", "instagram_account_id": "17841478010207208"}]
            ),
        )
    )
    db.flush()


def _fake_decrypt(_envelope):
    return {
        "user_access_token": "user-token",
        "page_access_tokens": {CANARY_FACEBOOK_PAGE_ID: "page-token"},
    }


def _patch_preflight_adapter(monkeypatch):
    monkeypatch.setattr(
        MetaProviderAdapter,
        "list_user_permissions",
        lambda self, **k: {
            "ok": True,
            "granted": ["pages_manage_posts", "pages_show_list", "pages_read_engagement", "public_profile"],
        },
    )
    monkeypatch.setattr(MetaProviderAdapter, "health_probe", lambda self, **k: {"ok": True, "health": "CONNECTED"})
    monkeypatch.setattr(
        MetaProviderAdapter,
        "debug_token",
        lambda self, **k: {
            "ok": True,
            "data": {
                "is_valid": True,
                "type": "PAGE",
                "profile_id": CANARY_FACEBOOK_PAGE_ID,
                "scopes": ["pages_manage_posts", "pages_show_list", "pages_read_engagement", "public_profile"],
            },
        },
    )


class RecordingAdapter(MetaProviderAdapter):
    def __init__(self):
        super().__init__()
        self.calls = []

    def list_user_permissions(self, *, access_token: str):
        return {"ok": True, "granted": ["pages_show_list", "pages_read_engagement", "pages_manage_posts", "public_profile"]}

    def health_probe(self, *, access_token: str, page_id: str | None = None):
        return {"ok": True, "health": "CONNECTED", "user_id": page_id or "x"}

    def debug_token(self, *, input_token: str):
        scopes = ["pages_show_list", "pages_read_engagement", "pages_manage_posts", "public_profile"]
        if input_token == "page-token":
            return {
                "ok": True,
                "data": {
                    "is_valid": True,
                    "type": "PAGE",
                    "profile_id": CANARY_FACEBOOK_PAGE_ID,
                    "scopes": scopes,
                },
            }
        return {"ok": True, "data": {"is_valid": True, "type": "USER", "scopes": scopes}}

    def publish_page_feed(self, **kwargs):
        self.calls.append(kwargs)
        # Delegate to real gate logic with stubbed HTTP.
        self.http = self._ok_http
        return super().publish_page_feed(**kwargs)

    @staticmethod
    def _ok_http(method, url, headers=None, body=None, timeout=20.0):
        assert method == "POST"
        assert "/feed" in url
        assert CANARY_FACEBOOK_PAGE_ID in url
        # body must not be logged by tests as token-bearing in assertions beyond presence
        assert body is not None
        return 200, {"id": f"{CANARY_FACEBOOK_PAGE_ID}_999888777"}


def test_disabled_denies_direct_provider(gates_disabled):
    adapter = MetaProviderAdapter(http=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no http")))
    auth = CanaryAuthorization(
        authorization_id=1,
        authorization_secret="secret",
        actor="a",
        workspace_id="default",
        page_id=CANARY_FACEBOOK_PAGE_ID,
        dry_run_id=1,
        payload_hash="h",
        idempotency_key="k",
        destination="facebook_page",
        execution_mode="controlled-canary",
        expires_at=datetime.utcnow() + timedelta(minutes=5),
    )
    out = adapter.publish_page_feed(
        page_id=CANARY_FACEBOOK_PAGE_ID,
        page_access_token="t",
        message=CANARY_MESSAGE,
        authorization=auth,
        facebook_gate_enabled=False,
        execution_mode="controlled-canary",
    )
    assert out["ok"] is False
    assert out["error"] == "FACEBOOK_PUBLISHING_DISABLED"
    assert out["http_posts"] == 0


def test_direct_provider_without_authorization_denies(gates_disabled):
    adapter = MetaProviderAdapter(http=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no http")))
    out = adapter.publish_page_feed(
        page_id=CANARY_FACEBOOK_PAGE_ID,
        page_access_token="t",
        message=CANARY_MESSAGE,
        authorization=None,
        facebook_gate_enabled=True,
        execution_mode="controlled-canary",
    )
    assert out["error"] == "CANARY_AUTHORIZATION_REQUIRED"
    assert out["provider_called"] is False


def test_dry_run_denies_provider_call(db_session, gates_disabled, monkeypatch):
    monkeypatch.setattr(canary, "_decrypt_credentials", lambda row: _fake_decrypt(""))
    out = canary.prepare_facebook_canary_dry_run(db_session, actor="op")
    assert out["ok"] and out["provider_http_posts"] == 0 and out["published"] is False
    assert out["payloads"][0]["would_send"] is False


def test_wrong_page_denies(db_session, gates_disabled, monkeypatch):
    monkeypatch.setattr(canary, "_decrypt_credentials", lambda row: _fake_decrypt(""))
    _seed_connection(db_session, page_id="111")
    dry = canary.prepare_facebook_canary_dry_run(db_session, actor="op")
    pre = canary.preflight_facebook_canary(
        db_session,
        actor="op",
        workspace_id="default",
        dry_run_id=dry["dry_run_id"],
        payload_hash=dry["payload_hash"],
        page_id=CANARY_FACEBOOK_PAGE_ID,
    )
    assert pre["ok"] is False
    assert any(c["check"] == "selected_page" and c["ok"] is False for c in pre["checks"])


def test_wrong_payload_hash_denies(db_session, gates_disabled, monkeypatch):
    monkeypatch.setattr(canary, "_decrypt_credentials", lambda row: _fake_decrypt(""))
    _seed_connection(db_session)
    dry = canary.prepare_facebook_canary_dry_run(db_session, actor="op")
    _patch_preflight_adapter(monkeypatch)
    pre = canary.preflight_facebook_canary(
        db_session,
        actor="op",
        workspace_id="default",
        dry_run_id=dry["dry_run_id"],
        payload_hash="deadbeef" * 8,
        page_id=CANARY_FACEBOOK_PAGE_ID,
    )
    assert pre["ok"] is False
    assert any(c["check"] == "payload_hash_match" and not c["ok"] for c in pre["checks"])


def test_expired_authorization_denies(db_session, gates_disabled):
    minted = mint_canary_authorization(
        db_session,
        actor="op",
        workspace_id="default",
        page_id=CANARY_FACEBOOK_PAGE_ID,
        dry_run_id=1,
        payload_hash="abc",
        idempotency_key="exp-1",
        explicit_approval="CONFIRM",
        ttl_seconds=1,
    )
    row = db_session.query(SocialPublishCanaryAuthorization).get(minted["authorization_id"])
    row.expires_at = datetime.utcnow() - timedelta(seconds=5)
    db_session.flush()
    out = canary.execute_facebook_canary(
        db_session,
        actor="op",
        workspace_id="default",
        authorization_id=minted["authorization_id"],
        authorization_secret=minted["authorization_secret"],
        dry_run_id=1,
        payload_hash="abc",
        page_id=CANARY_FACEBOOK_PAGE_ID,
        idempotency_key="exp-1",
    )
    assert out["ok"] is False
    assert out["error"] == "authorization_expired"
    assert out["provider_http_posts"] == 0


def test_boolean_authorization_rejected(db_session, gates_disabled, monkeypatch):
    monkeypatch.setattr(canary, "_decrypt_credentials", lambda row: _fake_decrypt(""))
    _seed_connection(db_session)
    dry = canary.prepare_facebook_canary_dry_run(db_session, actor="op")
    monkeypatch.setattr(
        MetaProviderAdapter,
        "list_user_permissions",
        lambda self, **k: {"ok": True, "granted": ["pages_manage_posts"]},
    )
    monkeypatch.setattr(MetaProviderAdapter, "health_probe", lambda self, **k: {"ok": True, "health": "CONNECTED"})
    out = canary.approve_and_mint_canary(
        db_session,
        actor="op",
        workspace_id="default",
        dry_run_id=dry["dry_run_id"],
        payload_hash=dry["payload_hash"],
        page_id=CANARY_FACEBOOK_PAGE_ID,
        explicit_approval=True,  # type: ignore[arg-type]
    )
    assert out["error"] == "boolean_authorization_rejected"


def test_instagram_remains_denied(gates_disabled):
    out = canary.deny_instagram_publish(enabled=True)
    assert out["ok"] is False
    assert out["instagram_posts_created"] == 0


def test_ai_assistant_cannot_bypass_confirmation(db_session, gates_disabled):
    out = services.chat_turn(
        db_session,
        actor="ai-user",
        perms=set(ALL_PERMISSIONS),
        conversation_id=None,
        message="Publish today's rates to Facebook",
    )
    tools = [t["tool"] for t in out["tool_calls"]]
    assert "publishing.facebook_canary" not in tools or all(
        t["result"].get("published") is not True for t in out["tool_calls"] if t["tool"] == "publishing.facebook_canary"
    )
    assert "Nothing was published" in out["reply"]
    assert out["tool_calls"][-1]["tool"] == "publishing.dry_run"
    assert out["tool_calls"][-1]["result"].get("published") is False


def test_exact_valid_canary_authorizes_one_call_only(db_session, gates_disabled, monkeypatch):
    monkeypatch.setattr(canary, "_decrypt_credentials", lambda row: _fake_decrypt(""))
    _seed_connection(db_session)
    adapter = RecordingAdapter()
    _patch_preflight_adapter(monkeypatch)

    dry = canary.prepare_facebook_canary_dry_run(db_session, actor="op")
    assert dry["provider_http_posts"] == 0
    pre = canary.preflight_facebook_canary(
        db_session,
        actor="op",
        workspace_id="default",
        dry_run_id=dry["dry_run_id"],
        payload_hash=dry["payload_hash"],
    )
    assert pre["ok"] is True
    assert pre["provider_http_posts"] == 0

    minted = canary.approve_and_mint_canary(
        db_session,
        actor="op",
        workspace_id="default",
        dry_run_id=dry["dry_run_id"],
        payload_hash=dry["payload_hash"],
        page_id=CANARY_FACEBOOK_PAGE_ID,
        explicit_approval="CONFIRM",
        idempotency_key="canary-idem-1",
    )
    assert minted["ok"]

    first = canary.execute_facebook_canary(
        db_session,
        actor="op",
        workspace_id="default",
        authorization_id=minted["authorization_id"],
        authorization_secret=minted["authorization_secret"],
        dry_run_id=dry["dry_run_id"],
        payload_hash=dry["payload_hash"],
        page_id=CANARY_FACEBOOK_PAGE_ID,
        idempotency_key="canary-idem-1",
        adapter=adapter,
    )
    assert first["ok"] is True
    assert first["provider_http_posts"] == 1
    assert first["external_post_id"]
    assert first["meta_facebook_publishing_enabled"] is False
    assert first["publishing_execution_mode"] == "disabled"
    assert len(adapter.calls) == 1

    # Reused authorization denied
    reuse = canary.execute_facebook_canary(
        db_session,
        actor="op",
        workspace_id="default",
        authorization_id=minted["authorization_id"],
        authorization_secret=minted["authorization_secret"],
        dry_run_id=dry["dry_run_id"],
        payload_hash=dry["payload_hash"],
        page_id=CANARY_FACEBOOK_PAGE_ID,
        idempotency_key="canary-idem-2",
        adapter=adapter,
    )
    assert reuse["ok"] is False
    assert reuse["error"] in {"authorization_reused", "authorization_used"}
    assert reuse["provider_http_posts"] == 0
    assert len(adapter.calls) == 1

    # Duplicate idempotency denies republish
    dup = canary.execute_facebook_canary(
        db_session,
        actor="op",
        workspace_id="default",
        authorization_id=minted["authorization_id"],
        authorization_secret=minted["authorization_secret"],
        dry_run_id=dry["dry_run_id"],
        payload_hash=dry["payload_hash"],
        page_id=CANARY_FACEBOOK_PAGE_ID,
        idempotency_key="canary-idem-1",
        adapter=adapter,
    )
    assert dup["ok"] is False
    assert dup["error"] == "duplicate_idempotency_key"
    assert len(adapter.calls) == 1

    assert db_session.query(SocialPublishReceipt).count() == 1
    assert db_session.query(SocialIdempotencyRecord).filter_by(idempotency_key="canary-idem-1").count() == 1


def test_live_mode_denied_by_provider(gates_disabled):
    adapter = MetaProviderAdapter(http=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no http")))
    auth = CanaryAuthorization(
        authorization_id=1,
        authorization_secret="s",
        actor="a",
        workspace_id="default",
        page_id=CANARY_FACEBOOK_PAGE_ID,
        dry_run_id=1,
        payload_hash="h",
        idempotency_key="k",
        destination="facebook_page",
        execution_mode="controlled-canary",
        expires_at=datetime.utcnow() + timedelta(minutes=5),
    )
    out = adapter.publish_page_feed(
        page_id=CANARY_FACEBOOK_PAGE_ID,
        page_access_token="t",
        message=CANARY_MESSAGE,
        authorization=auth,
        facebook_gate_enabled=True,
        execution_mode=PublishingExecutionMode.LIVE.value,
    )
    assert out["error"] == "EXECUTION_MODE_DENIED"


def test_tool_registry_general_publish_unavailable():
    assert get_tool("publishing.publish").available is False
    assert get_tool("publishing.facebook_canary").available is True
    assert get_tool("publishing.facebook_canary").confirmation_required is True
