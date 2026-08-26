"""Real operator bugfix contracts: media, mentions, timezone, presentation."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from PIL import Image


def test_awake_window_local_pickups_never_outside_10_20(monkeypatch):
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
    assert sched["local_times_json"] == ["10:00", "20:00"]
    assert sched["times_json"] == ["06:00", "16:00"]

    executions = format_execution_times(
        sched["times_json"],
        start_at=datetime(2026, 8, 18, 12, 0, 0),
        duration_days=3,
        tz_name="Asia/Yerevan",
    )
    assert executions, "expected pickup slots"
    for row in executions:
        assert row["local_time"] in {"10:00", "20:00"}, row
    check = local_pickups_within_awake_window(executions)
    assert check["ok"] is True
    assert check["violations"] == []

    local = datetime(2026, 8, 19, 14, 0, tzinfo=ZoneInfo("Asia/Yerevan"))
    assert local.astimezone(timezone.utc).strftime("%H:%M") == "10:00"


def test_mentions_forced_off_when_uncertified(monkeypatch):
    monkeypatch.setenv("AUTOSTORY_MENTIONS_PRODUCTION_CERTIFIED", "false")
    from src.stories.autostory_media import normalize_campaign_mentions

    n = normalize_campaign_mentions(3)
    assert n["mentions_per_story"] == 0
    assert n["mentions_enabled"] is False
    assert n["mentions_forced_off"] is True
    assert n["mentions_production_certified"] is False


def test_mentions_allowed_when_certified(monkeypatch):
    monkeypatch.setenv("AUTOSTORY_MENTIONS_PRODUCTION_CERTIFIED", "true")
    from src.stories.autostory_media import normalize_campaign_mentions

    n = normalize_campaign_mentions(3)
    assert n["mentions_per_story"] == 3
    assert n["mentions_enabled"] is True
    assert n["mentions_forced_off"] is False


def test_incompatible_media_blocks_validation(tmp_path, monkeypatch):
    media = tmp_path / "media"
    media.mkdir()
    bad = media / "tiny_rgba.png"
    Image.new("RGBA", (234, 238), color=(255, 0, 0, 128)).save(bad)
    monkeypatch.setattr("src.stories.autostory_media.media_dir", lambda: media.resolve())
    from src.stories.autostory_media import validate_campaign_media

    r = validate_campaign_media(str(bad))
    assert r["ok"] is False
    assert r.get("compat_blocker")


def test_story_ready_media_validates(tmp_path, monkeypatch):
    media = tmp_path / "media"
    media.mkdir()
    path = media / "operator_ready_1080x1920.jpg"
    Image.new("RGB", (1080, 1920), color=(40, 80, 120)).save(path, format="JPEG", quality=90)
    monkeypatch.setattr("src.stories.autostory_media.media_dir", lambda: media.resolve())
    from src.stories.autostory_media import validate_campaign_media

    r = validate_campaign_media(str(path))
    assert r["ok"] is True, r


def test_missing_media_blocks_validation(tmp_path, monkeypatch):
    media = tmp_path / "media"
    media.mkdir()
    monkeypatch.setattr("src.stories.autostory_media.media_dir", lambda: media.resolve())
    from src.stories.autostory_media import validate_campaign_media

    r = validate_campaign_media("does_not_exist.png")
    assert r["ok"] is False


def test_presentation_needs_attention_for_media_error():
    from src.stories.auto_story_service import campaign_presentation_status

    p = campaign_presentation_status(
        {"status": "active", "last_error": "missing_or_invalid_media"}
    )
    assert p["label"] == "Needs Attention"
    assert p["needs_attention"] is True
    assert "media" in (p["detail"] or "").lower()


def test_normalize_times_awake_uses_operator_tz(monkeypatch):
    monkeypatch.setattr(
        "src.stories.autostory_operator_preview.operator_timezone_name",
        lambda: "Asia/Yerevan",
    )
    from src.stories.autostory_operator_preview import normalize_times_json

    sched = normalize_times_json(None, posts_per_day=2)
    assert sched["schedule_mode"] == "awake_window"
    assert sched["times_json"] == ["06:00", "16:00"]
    assert "00:00" not in sched.get("local_times_json", [])
