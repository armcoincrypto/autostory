"""Meta publishing dry-run pipeline — preview only; provider never called."""
from __future__ import annotations

import hashlib
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.database import Base
import src.social_agent.models  # noqa: F401
from src.social_agent.models import SocialAgentAuditEvent, SocialConnection, SocialPublishDryRun
from src.social_agent.permissions import ALL_PERMISSIONS
from src.social_agent import services
from src.social_agent.publishing.service import (
    get_dry_run_preview,
    list_dry_run_history,
    run_publishing_dry_run,
)
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
def http_trap(monkeypatch):
    """Fail the test if any HTTP client is invoked during dry-run."""

    def boom(*_a, **_k):
        raise AssertionError("HTTP must not be called during publishing dry-run")

    for mod_name in ("urllib.request", "requests", "http.client"):
        try:
            mod = __import__(mod_name, fromlist=["*"])
        except ImportError:
            continue
        for attr in ("urlopen", "request", "get", "post", "put", "patch", "delete", "HTTPSConnection", "HTTPConnection"):
            if hasattr(mod, attr):
                monkeypatch.setattr(mod, attr, boom, raising=False)

    try:
        import src.social_agent.providers.meta as meta_mod

        for name in dir(meta_mod.MetaProviderAdapter):
            if name.startswith("_"):
                continue
            attr = getattr(meta_mod.MetaProviderAdapter, name, None)
            if callable(attr):
                monkeypatch.setattr(meta_mod.MetaProviderAdapter, name, boom, raising=False)
    except Exception:
        pass
    return True


def _content_fb():
    return {
        "text": "Today's USDT → AMD rates are live.",
        "hashtags": ["Exswaping", "USDT"],
        "link": "https://exswaping.com",
        "language": "EN",
        "brand_voice": "clear, trustworthy",
    }


def _content_ig_feed():
    return {
        "text": "Rates update",
        "hashtags": ["Exswaping"],
        "images": [{"url": "https://cdn.example.com/rates.jpg"}],
        "language": "EN",
    }


def _content_carousel():
    return {
        "text": "Carousel rates",
        "images": [
            {"url": "https://cdn.example.com/1.jpg"},
            {"url": "https://cdn.example.com/2.jpg"},
            {"url": "https://cdn.example.com/3.jpg"},
        ],
    }


def _content_story():
    return {"images": [{"url": "https://cdn.example.com/story.jpg"}], "text": "ignored caption"}


def test_facebook_preview(db_session, http_trap):
    out = run_publishing_dry_run(
        db_session,
        actor="tester",
        destinations=["facebook_page"],
        content=_content_fb(),
    )
    assert out["ok"] and out["dry_run"] and out["published"] is False
    assert out["provider_called"] is False
    assert out["provider_http_posts"] == 0
    assert out["meta_provider_mutations"] == 0
    assert out["validation"]["ok"] is True
    assert len(out["payloads"]) == 1
    p = out["payloads"][0]
    assert p["destination"] == "facebook_page"
    assert p["would_send"] is False
    assert p["provider_called"] is False
    assert "access_token" not in json.dumps(p).lower()
    assert "page_access_token" not in json.dumps(p).lower()
    assert "/feed" in p["endpoint"] or "/photos" in p["endpoint"]


def test_instagram_feed_preview(db_session, http_trap):
    out = run_publishing_dry_run(
        db_session,
        actor="tester",
        destinations=["instagram_feed"],
        content=_content_ig_feed(),
    )
    assert out["validation"]["ok"] is True
    p = out["payloads"][0]
    assert p["destination"] == "instagram_feed"
    assert p["steps"][0]["endpoint"].endswith("/media")
    assert p["steps"][1]["endpoint"].endswith("/media_publish")
    assert p["would_send"] is False


def test_instagram_carousel_preview(db_session, http_trap):
    out = run_publishing_dry_run(
        db_session,
        actor="tester",
        destinations=["instagram_carousel"],
        content=_content_carousel(),
    )
    assert out["validation"]["ok"] is True
    p = out["payloads"][0]
    assert p["destination"] == "instagram_carousel"
    assert any(s["name"] == "create_carousel_container" for s in p["steps"])
    assert any(s["name"] == "publish_carousel" for s in p["steps"])


def test_instagram_story_preview(db_session, http_trap):
    out = run_publishing_dry_run(
        db_session,
        actor="tester",
        destinations=["instagram_story"],
        content=_content_story(),
    )
    assert out["validation"]["ok"] is True
    p = out["payloads"][0]
    assert p["destination"] == "instagram_story"
    assert p["steps"][0]["body"]["media_type"] == "STORIES"
    assert p["would_send"] is False


def test_validation_failures_missing_destination(db_session, http_trap):
    out = run_publishing_dry_run(db_session, actor="tester", destinations=[], content=_content_fb())
    assert out["validation"]["ok"] is False
    assert any(e["code"] == "missing_destination" for e in out["validation"]["errors"])
    assert out["payloads"] == []
    assert out["published"] is False


def test_validation_wrong_media(db_session, http_trap):
    out = run_publishing_dry_run(
        db_session,
        actor="tester",
        destinations=["instagram_feed"],
        content={"text": "no media"},
    )
    assert out["validation"]["ok"] is False
    dest = out["validation"]["destinations"][0]
    assert any(e["code"] == "media_required" for e in dest["errors"])
    assert out["payloads"] == []


def test_invalid_hashtag(db_session, http_trap):
    out = run_publishing_dry_run(
        db_session,
        actor="tester",
        destinations=["facebook_page"],
        content={"text": "hi", "hashtags": ["bad tag!"]},
    )
    assert out["validation"]["ok"] is False
    assert any(
        e["code"] == "invalid_hashtag"
        for d in out["validation"]["destinations"]
        for e in d["errors"]
    )


def test_payload_hashing_stable(db_session, http_trap):
    a = run_publishing_dry_run(db_session, actor="a", destinations=["facebook_page"], content=_content_fb())
    b = run_publishing_dry_run(db_session, actor="b", destinations=["facebook_page"], content=_content_fb())
    assert a["payload_hash"] == b["payload_hash"]
    assert len(a["payload_hash"]) == 64
    # Manual recompute
    canonical = json.dumps(a["payloads"], sort_keys=True, separators=(",", ":"), default=str)
    assert hashlib.sha256(canonical.encode()).hexdigest() == a["payload_hash"]


def test_audit_rows(db_session, http_trap):
    out = run_publishing_dry_run(
        db_session,
        actor="auditor",
        destinations=["facebook_page"],
        content=_content_fb(),
        workspace_id="default",
    )
    row = db_session.query(SocialPublishDryRun).filter_by(id=out["dry_run_id"]).one()
    assert row.actor == "auditor"
    assert row.payload_hash == out["payload_hash"]
    assert row.provider_called is False
    assert row.provider_http_posts == 0
    assert "access_token" not in (row.payloads_json or "").lower()
    assert "page_access_token" not in (row.payloads_json or "").lower()
    assert "app_secret" not in (row.payloads_json or "").lower()
    events = (
        db_session.query(SocialAgentAuditEvent)
        .filter(SocialAgentAuditEvent.action == "publishing.dry_run")
        .all()
    )
    assert events
    detail = json.loads(events[-1].detail_json or "{}")
    assert detail["payload_hash"] == out["payload_hash"]
    assert detail["provider_called"] is False
    assert detail["workspace_id"] == "default"


def test_history_and_preview_get(db_session, http_trap):
    out = run_publishing_dry_run(
        db_session,
        actor="hist",
        destinations=["facebook_page", "instagram_feed"],
        content={**_content_ig_feed(), **{"link": None}},
    )
    hist = list_dry_run_history(db_session)
    assert hist["ok"]
    assert any(i["id"] == out["dry_run_id"] for i in hist["items"])
    prev = get_dry_run_preview(db_session, out["dry_run_id"])
    assert prev["ok"]
    assert prev["dry_run_id"] == out["dry_run_id"]
    assert prev["published"] is False
    assert prev["payload_hash"] == out["payload_hash"]


def test_ai_assistant_publish_command(db_session, http_trap):
    out = services.chat_turn(
        db_session,
        actor="assistant-user",
        perms=set(ALL_PERMISSIONS),
        conversation_id=None,
        message="Publish today's rates to Facebook",
    )
    assert out["ok"]
    tools = [t["tool"] for t in out["tool_calls"]]
    assert "content.generate_variants" in tools
    assert "publishing.dry_run" in tools
    dry = next(t["result"] for t in out["tool_calls"] if t["tool"] == "publishing.dry_run")
    assert dry["published"] is False
    assert dry["provider_called"] is False
    assert "Published" not in out["reply"] or "Nothing was published" in out["reply"]
    assert "Draft created" in out["reply"]
    assert "Validation" in out["reply"]
    assert "Ready for publishing" in out["reply"] or "Not ready" in out["reply"]
    assert "Nothing was published" in out["reply"]


def test_tool_registry_dry_run_available():
    t = get_tool("publishing.dry_run")
    assert t is not None and t.available is True
    pub = get_tool("publishing.publish")
    assert pub is not None and pub.available is False


def test_provider_never_called_even_with_connection(db_session, http_trap):
    db_session.add(
        SocialConnection(
            provider="meta",
            workspace_id="default",
            display_name="Exswaping",
            status="connected",
            health="healthy",
            selected_page_id="867560236439580",
            selected_instagram_id="17841478010207208",
            destinations_json=json.dumps(
                [
                    {
                        "page_id": "867560236439580",
                        "page_name": "Exswaping",
                        "instagram_username": "exswaping",
                    }
                ]
            ),
        )
    )
    db_session.flush()
    out = run_publishing_dry_run(
        db_session,
        actor="conn",
        destinations=["facebook_page", "instagram_feed"],
        content=_content_ig_feed(),
    )
    assert out["provider_called"] is False
    assert out["meta_provider_mutations"] == 0
    for p in out["payloads"]:
        assert p["provider_called"] is False
        assert p["would_send"] is False
        assert "867560236439580" in json.dumps(p) or "17841478010207208" in json.dumps(p)


def test_ui_publishing_page_renders():
    from src.dashboard.app import create_app

    app = create_app()
    client = app.test_client()
    # Unauthenticated should redirect/401 — page exists in map.
    paths = {r.rule for r in app.url_map.iter_rules()}
    assert "/social-agent/publishing" in paths
    res = client.get("/social-agent/publishing", follow_redirects=False)
    assert res.status_code in {302, 401}


def test_api_dry_run_history_routes_auth():
    from src.dashboard.app import create_app

    app = create_app()
    client = app.test_client()
    for path, method in (
        ("/api/v1/social-agent/publishing/dry-run", "post"),
        ("/api/v1/social-agent/publishing/history", "get"),
        ("/api/v1/social-agent/publishing/preview/1", "get"),
    ):
        res = getattr(client, method)(path, json={} if method == "post" else None)
        assert res.status_code == 401


def test_no_live_publish_endpoint():
    from src.dashboard.app import create_app

    app = create_app()
    paths = {r.rule for r in app.url_map.iter_rules()}
    assert "/api/v1/social-agent/publishing/publish" not in paths
    # Meta publish route remains hard 403 denial.
    assert any("connections/meta/publish" in p for p in paths)
