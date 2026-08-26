"""Legacy policy gate + media prepare + presentation status."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PIL import Image


def test_legacy_mentions_blocked_when_uncertified(monkeypatch):
    monkeypatch.setenv("AUTOSTORY_MENTIONS_PRODUCTION_CERTIFIED", "false")
    from src.stories.autostory_media import validate_campaign_execution_policy

    monkeypatch.setattr(
        "src.stories.autostory_media.validate_campaign_media",
        lambda path: {"ok": True, "path": path, "error": None, "needs_preparation": False},
    )
    r = validate_campaign_execution_policy(
        {
            "status": "active",
            "media_path": "/opt/autostory/data/media/x.jpg",
            "mentions_per_story": 3,
            "account_ids": [1, 2, 3],
            "times_json": ["06:00"],
        }
    )
    assert r["ok"] is False
    assert r["error"] == "BLOCKED_POLICY"
    assert r["reason"] == "mentions_not_production_certified"
    assert r["classification"] == "INVALID_MENTIONS"


def test_policy_change_after_schedule_blocks_execution(monkeypatch):
    from src.stories.autostory_media import validate_campaign_execution_policy

    monkeypatch.setattr(
        "src.stories.autostory_media.validate_campaign_media",
        lambda path: {"ok": True, "path": path, "error": None},
    )
    camp = {
        "status": "active",
        "media_path": "ok.jpg",
        "mentions_per_story": 2,
        "account_ids": [10],
        "times_json": ["12:00"],
    }
    monkeypatch.setenv("AUTOSTORY_MENTIONS_PRODUCTION_CERTIFIED", "true")
    assert validate_campaign_execution_policy(camp)["ok"] is True

    monkeypatch.setenv("AUTOSTORY_MENTIONS_PRODUCTION_CERTIFIED", "false")
    blocked = validate_campaign_execution_policy(camp)
    assert blocked["ok"] is False
    assert blocked["reason"] == "mentions_not_production_certified"


def test_prepare_story_derivative_rgba(tmp_path, monkeypatch):
    media = tmp_path / "media"
    media.mkdir()
    src = media / "tiny_rgba.png"
    Image.new("RGBA", (200, 300), color=(255, 0, 0, 128)).save(src)
    monkeypatch.setattr("src.stories.autostory_media.media_dir", lambda: media.resolve())
    from src.stories.autostory_media import prepare_story_derivative, validate_campaign_media

    before = src.read_bytes()
    out = prepare_story_derivative(str(src))
    assert out["ok"] is True, out
    assert Path(out["absolute_path"]).is_file()
    assert src.read_bytes() == before  # source unchanged
    assert validate_campaign_media(out["path"])["ok"] is True
    assert out.get("width") == 1080
    assert out.get("height") == 1920


def test_prepare_already_ready_skips_rewrite(tmp_path, monkeypatch):
    media = tmp_path / "media"
    media.mkdir()
    src = media / "ready.jpg"
    Image.new("RGB", (1080, 1920), color=(10, 20, 30)).save(src, format="JPEG", quality=90)
    monkeypatch.setattr("src.stories.autostory_media.media_dir", lambda: media.resolve())
    from src.stories.autostory_media import prepare_story_derivative

    out = prepare_story_derivative(str(src))
    assert out["ok"] is True
    assert out.get("already_ready") is True


def test_presentation_scheduled_waiting_needs_attention(monkeypatch):
    monkeypatch.setenv("AUTOSTORY_MENTIONS_PRODUCTION_CERTIFIED", "false")
    from src.stories.auto_story_service import campaign_presentation_status

    monkeypatch.setattr(
        "src.stories.autostory_media.validate_campaign_media",
        lambda path: {"ok": True, "path": path, "error": None},
    )

    future = (datetime.utcnow() + timedelta(days=1)).isoformat()
    scheduled = campaign_presentation_status(
        {
            "status": "active",
            "last_error": None,
            "claimed_by": None,
            "next_wave_at": future,
            "mentions_per_story": 0,
            "media_path": "x.jpg",
            "account_ids": [1],
            "times_json": ["06:00"],
        }
    )
    assert scheduled["label"] == "Scheduled"

    running = campaign_presentation_status(
        {
            "status": "active",
            "last_error": None,
            "claimed_by": "worker-1",
            "next_wave_at": future,
            "mentions_per_story": 0,
            "media_path": "x.jpg",
            "account_ids": [1],
            "times_json": ["06:00"],
        }
    )
    assert running["label"] == "Running"

    attn = campaign_presentation_status(
        {
            "status": "active",
            "last_error": None,
            "claimed_by": None,
            "next_wave_at": future,
            "mentions_per_story": 3,
            "media_path": "x.jpg",
            "account_ids": [1],
            "times_json": ["06:00"],
        }
    )
    assert attn["label"] == "Needs Attention"
    assert attn["needs_attention"] is True
    assert "Mentions" in (attn["detail"] or "")


def test_awake_local_times_still_in_window(monkeypatch):
    monkeypatch.setattr(
        "src.stories.autostory_operator_preview.operator_timezone_name",
        lambda: "Asia/Yerevan",
    )
    from src.stories.autostory_operator_preview import (
        awake_window_times_utc,
        format_execution_times,
        local_pickups_within_awake_window,
    )

    sched = awake_window_times_utc(posts_per_day=2, tz_name="Asia/Yerevan")
    rows = format_execution_times(
        sched["times_json"],
        start_at=datetime(2026, 8, 18, 12, 0, 0),
        duration_days=3,
        tz_name="Asia/Yerevan",
    )
    assert local_pickups_within_awake_window(rows)["ok"] is True
