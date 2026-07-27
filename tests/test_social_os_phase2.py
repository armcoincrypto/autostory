"""Phase 2 Social OS — content studio, media, calendar, brand, surfaces, copilot."""
from __future__ import annotations

import io

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.database import Base
import src.social_agent.models  # noqa: F401
from src.social_agent import services
from src.social_agent import content_workflow as cw
from src.social_agent import media_library as media
from src.social_agent import brand_store as brand
from src.social_agent import calendar_service as cal
from src.social_agent import surfaces
from src.social_agent import copilot
from src.social_agent.platforms import DEFAULT_VARIANT_PLATFORMS, PLATFORM_LIMITS
from src.social_agent.tools import get_tool
from src.social_agent.permissions import ALL_PERMISSIONS


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
def perms():
    return set(ALL_PERMISSIONS)


def test_variant_platforms_include_studio_set():
    assert "tiktok" in DEFAULT_VARIANT_PLATFORMS
    assert "youtube_community" in DEFAULT_VARIANT_PLATFORMS
    assert "discord" in DEFAULT_VARIANT_PLATFORMS
    assert PLATFORM_LIMITS["x"] == 280


def test_generate_all_studio_platforms(db_session, perms):
    out = services.execute_tool(
        db_session,
        actor="tester",
        perms=perms,
        tool_name="content.generate_variants",
        arguments={
            "brief": "USDT to AMD rates update",
            "platforms": list(DEFAULT_VARIANT_PLATFORMS),
            "languages": ["EN"],
            "persist": True,
        },
        dry_run=False,
    )
    assert out["ok"]
    plats = {v["platform"] for v in out["variants"]}
    assert plats == set(DEFAULT_VARIANT_PLATFORMS)
    preview = cw.studio_previews(db_session, content_id=out["content_id"])
    assert preview["ok"]
    assert len(preview["previews"]) == len(DEFAULT_VARIANT_PLATFORMS)
    assert preview["provider_called"] is False


def test_approval_workflow_and_publish_locked(db_session, perms):
    created = services.create_draft(
        db_session,
        actor="tester",
        title="Rates",
        brief="Brief",
        body="Body text",
        platforms=["facebook"],
        languages=["EN"],
    )
    cid = created["content_id"]
    to_review = cw.transition_status(db_session, actor="tester", content_id=cid, to_status="NEEDS_REVIEW")
    assert to_review["ok"]
    approved = cw.transition_status(db_session, actor="tester", content_id=cid, to_status="APPROVED")
    assert approved["ok"]
    published = cw.transition_status(db_session, actor="tester", content_id=cid, to_status="PUBLISHED")
    assert published["ok"] is False
    assert published["error"] == "publish_status_locked"
    events = cw.list_status_events(db_session, cid)
    assert len(events["events"]) == 2


def test_media_upload_dedupe_and_safe_delete(db_session, tmp_path, monkeypatch):
    monkeypatch.setenv("SOCIAL_MEDIA_LIBRARY_ROOT", str(tmp_path))
    payload = b"\xff\xd8\xff" + b"fake-jpeg-bytes"
    first = media.upload_asset(
        db_session,
        actor="tester",
        filename="a.jpg",
        stream=io.BytesIO(payload),
        mime_type="image/jpeg",
    )
    assert first["ok"] and not first["deduplicated"]
    second = media.upload_asset(
        db_session,
        actor="tester",
        filename="b.jpg",
        stream=io.BytesIO(payload),
        mime_type="image/jpeg",
    )
    assert second["ok"] and second["deduplicated"] is True
    listed = media.list_assets(db_session)
    assert len(listed["assets"]) == 1
    deleted = media.soft_delete_asset(db_session, actor="tester", asset_id=first["asset"]["id"])
    assert deleted["ok"]


def test_brand_seed_and_ai_context(db_session):
    listed = brand.list_knowledge(db_session)
    assert listed["ok"]
    cats = {i["category"] for i in listed["items"]}
    assert "voice" in cats and "banned" in cats and "legal" in cats
    ctx = brand.brand_context_for_ai(db_session)
    assert "voice" in ctx["brand"]


def test_calendar_queue_no_live_publish(db_session):
    created = services.create_draft(
        db_session,
        actor="tester",
        title="Cal",
        brief="Hello",
        body="Hello calendar",
        platforms=["facebook"],
    )
    out = cal.create_entry(
        db_session,
        actor="tester",
        content_id=created["content_id"],
        platform="facebook",
        scheduled_for="2026-08-01T12:00:00Z",
        timezone_name="UTC",
    )
    assert out["ok"]
    assert out["live_publish_enabled"] is False
    listed = cal.list_entries(db_session, view="agenda", anchor="2026-08-01T00:00:00Z")
    assert listed["live_publish_enabled"] is False
    assert any(e["id"] == out["entry"]["id"] for e in listed["entries"])


def test_surfaces_honest_empty(db_session):
    a = surfaces.analytics_architecture(db_session)
    assert a["message"] == "No analytics available."
    assert all(c["value"] is None for c in a["cards"])
    c = surfaces.comments_architecture()
    assert c["threads"] == []
    m = surfaces.messages_architecture()
    assert m["conversations"] == []
    auto = surfaces.save_automation_draft(db_session, actor="tester", name="Demo", definition={"triggers": []})
    assert auto["enabled"] is False
    listed = surfaces.automations_architecture(db_session)
    assert listed["live_automation_enabled"] is False


def test_accounts_matrix_no_fake_providers(db_session):
    matrix = surfaces.accounts_provider_matrix(db_session)
    by = {p["provider"]: p for p in matrix["providers"]}
    assert by["x"]["status"] == "NOT CONFIGURED"
    assert by["linkedin"]["status"] == "NOT CONFIGURED"
    assert matrix["fabricated"] is False


def test_copilot_never_publishes(db_session):
    out = copilot.assist(db_session, intent="rewrite", text="hello world")
    assert out["auto_publish"] is False
    assert out["ok"]
    tags = copilot.suggest_hashtags(db_session, "USDT rates")
    assert tags["ok"]
    compliance = copilot.compliance_check(db_session, "guaranteed returns forever")
    assert compliance["compliant"] is False


def test_live_publish_tool_still_unavailable():
    pub = get_tool("publishing.publish")
    assert pub is not None and pub.available is False
    sched = get_tool("publishing.schedule")
    assert sched.available is False


def test_chat_copilot_loads_brand(db_session, perms):
    out = services.chat_turn(
        db_session,
        actor="tester",
        perms=perms,
        conversation_id=None,
        message="What can you do?",
    )
    assert out["ok"]
    assert "never publish" in out["reply"].lower() or "Never publish" in out["reply"] or "never publishes" in out["reply"].lower()
    assert "Brand categories loaded" in out["reply"]
