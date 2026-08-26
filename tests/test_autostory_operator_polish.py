"""Operator polish: schedule preview truth, fleet summary, structured logs."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from src.stories.auto_story_service import preview_schedule
from src.stories.autostory_operator_preview import (
    _safe_fields,
    format_execution_times,
    log_autostory_event,
    normalize_times_json,
    plan_waves_for_accounts,
)


def test_explicit_times_json_not_awake_window() -> None:
    sched = normalize_times_json(["09:00", "13:30"], posts_per_day=1)
    assert sched["explicit"] is True
    assert sched["schedule_mode"] == "explicit_times"
    assert sched["times_json"] == ["09:00", "13:30"]
    assert sched.get("awake_window") is None


def test_awake_window_when_no_times(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.stories.autostory_operator_preview.operator_timezone_name",
        lambda: "Asia/Yerevan",
    )
    sched = normalize_times_json(None, posts_per_day=2)
    assert sched["explicit"] is False
    assert sched["schedule_mode"] == "awake_window"
    # Local awake 10:00/20:00 Asia/Yerevan → UTC 06:00/16:00
    assert sched["local_times_json"] == ["10:00", "20:00"]
    assert sched["times_json"] == ["06:00", "16:00"]
    assert sched["awake_window"] == "10:00-20:00"


def test_iso_times_normalize_to_hhmm_utc() -> None:
    sched = normalize_times_json(
        ["2026-08-18T09:00:00Z", "2026-08-18T13:30:00Z"],
        posts_per_day=1,
    )
    assert sched["explicit"] is True
    assert sched["times_json"] == ["09:00", "13:30"]


def test_preview_schedule_honors_explicit_times() -> None:
    p = preview_schedule(
        duration_days=1,
        posts_per_day=1,
        times_json=["09:00", "13:30"],
    )
    assert p["times_json"] == ["09:00", "13:30"]
    assert p.get("explicit_times") is True
    assert p.get("schedule_mode") == "explicit_times"
    assert p.get("awake_window") in (None,)
    # Must not silently become awake default 15:00
    assert p["times_json"] != ["15:00"]


def test_preview_schedule_awake_still_works(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.stories.autostory_operator_preview.operator_timezone_name",
        lambda: "Asia/Yerevan",
    )

    class _Boom:
        def __enter__(self):
            raise RuntimeError("isolated_preview_no_db")

        def __exit__(self, *args):
            return False

    monkeypatch.setattr("src.stories.auto_story_service.get_db_context", lambda: _Boom())
    p = preview_schedule(duration_days=5, posts_per_day=3)
    assert p["times_json"] == ["06:00", "11:00", "16:00"]
    assert p.get("schedule_mode") == "awake_window"
    assert p.get("awake_window") == "10:00-20:00"


def test_timezone_conversion_labels() -> None:
    rows = format_execution_times(
        ["09:00"],
        start_at=datetime(2026, 8, 17, 8, 0, 0),
        ends_at=datetime(2026, 8, 19, 0, 0, 0),
        duration_days=1,
        tz_name="Europe/Moscow",
    )
    assert rows
    assert "UTC" in rows[0]["utc_label"]
    assert "Europe/Moscow" in rows[0]["local_label"]
    assert rows[0]["hhmm_utc"] == "09:00"


def test_wave_plan_splits_over_25() -> None:
    db = MagicMock()
    # rotate_account_ids returns input order when last_story missing — stub via side effect
    from src.stories import autostory_operator_preview as mod

    orig = mod.rotate_account_ids
    mod.rotate_account_ids = lambda _db, ids: list(ids)
    try:
        plan = plan_waves_for_accounts(db, list(range(1, 61)))
        assert plan["account_count"] == 60
        assert plan["wave_count"] == 3
        assert [w["size"] for w in plan["waves"]] == [25, 25, 10]
        assert plan["max_wave_size"] == 25
        assert plan["any_over_max"] is False
    finally:
        mod.rotate_account_ids = orig


def test_secret_redaction_in_log_fields() -> None:
    safe = _safe_fields(
        {
            "campaign_id": 12,
            "session_string": "SECRET",
            "phone": "+1555",
            "api_hash": "hash",
            "account_id": 151,
        }
    )
    assert "session_string" not in safe
    assert "phone" not in safe
    assert "api_hash" not in safe
    assert safe["campaign_id"] == 12
    assert safe["account_id"] == 151


def test_log_autostory_event_does_not_raise(caplog) -> None:
    log_autostory_event(
        "autostory.campaign.claimed",
        campaign_id=12,
        worker_id="scheduler-1",
        accounts=20,
        session_string="should-not-appear",
    )
