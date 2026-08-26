"""Final admin UX contract tests: mentions, accounts, multi-day schedule, semantics."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

from src.stories.autostory_operator_preview import (
    build_operator_campaign_preview,
    format_execution_times,
    normalize_times_json,
)
from src.stories.auto_story_service import preview_schedule


def test_mentions_default_off_in_preview(monkeypatch):
    from src.stories import autostory_operator_preview as mod

    monkeypatch.setattr(
        mod,
        "fleet_autostory_summary",
        lambda db: {
            "production_ready": 94,
            "available_today": 94,
            "eligible_now": 94,
            "at_daily_limit": 0,
            "temporarily_blocked": 0,
            "diagnostics": {"auth_refresh_before_publish": 0},
            "help": "",
            "_eligible_all_ids": list(range(1, 95)),
        },
    )
    monkeypatch.setattr(mod, "select_automatic_accounts", lambda db, requested, exclude_ids=None: {
        "selection_mode": "automatic",
        "requested": "25",
        "requested_count": 25,
        "selected_account_ids": list(range(1, 26)),
        "selected_count": 25,
        "eligible_available": 94,
        "shortfall": 0,
        "production_ready": 94,
        "acknowledgment_required": False,
        "message": None,
    })
    monkeypatch.setattr(mod, "plan_waves_for_accounts", lambda db, ids: {
        "account_count": len(ids), "wave_count": 1,
        "waves": [{"index": 1, "size": len(ids), "account_ids": ids}],
        "max_wave_size": 25, "duplicates": 0, "any_over_max": False,
    })
    p = build_operator_campaign_preview(MagicMock(), {
        "media_path": "/tmp/x.jpg",
        "selection_mode": "automatic",
        "automatic_count": 25,
        "duration_days": 1,
        "posts_per_day": 1,
        "times_json": ["12:00"],
    })
    assert p["mentions_per_story"] == 0
    assert p["mentions_off"] is True


def test_requested_vs_planned_shortfall_blocks_without_allow_fewer(monkeypatch):
    from src.stories import autostory_operator_preview as mod

    monkeypatch.setattr(
        mod,
        "fleet_autostory_summary",
        lambda db: {
            "production_ready": 94,
            "available_today": 20,
            "eligible_now": 20,
            "at_daily_limit": 0,
            "temporarily_blocked": 0,
            "diagnostics": {},
            "help": "",
            "_eligible_all_ids": list(range(1, 21)),
        },
    )
    monkeypatch.setattr(mod, "select_automatic_accounts", lambda db, requested, exclude_ids=None: {
        "selection_mode": "automatic",
        "requested": "25",
        "requested_count": 25,
        "selected_account_ids": list(range(1, 21)),
        "selected_count": 20,
        "eligible_available": 20,
        "shortfall": 5,
        "production_ready": 94,
        "acknowledgment_required": True,
        "message": "Only 20 accounts are currently eligible. Requested: 25.",
    })
    monkeypatch.setattr(mod, "plan_waves_for_accounts", lambda db, ids: {
        "account_count": 0, "wave_count": 0, "waves": [],
        "max_wave_size": 25, "duplicates": 0, "any_over_max": False,
    })
    monkeypatch.setattr(
        "src.stories.autostory_media.validate_campaign_media",
        lambda path: {
            "ok": True,
            "media_ok": True,
            "path": path,
            "filename": "x.jpg",
            "message": "Media ready.",
        },
    )
    blocked = build_operator_campaign_preview(MagicMock(), {
        "media_path": "/tmp/x.jpg",
        "selection_mode": "automatic",
        "automatic_count": 25,
        "allow_fewer": False,
        "times_json": ["12:00"],
        "duration_days": 1,
        "posts_per_day": 1,
    })
    assert blocked["approval_blocked"] is True
    assert blocked["planned_count"] == 0
    assert blocked["requested_count"] == 25

    monkeypatch.setattr(mod, "plan_waves_for_accounts", lambda db, ids: {
        "account_count": len(ids), "wave_count": 1,
        "waves": [{"index": 1, "size": len(ids), "account_ids": ids}],
        "max_wave_size": 25, "duplicates": 0, "any_over_max": False,
    })
    allowed = build_operator_campaign_preview(MagicMock(), {
        "media_path": "/tmp/x.jpg",
        "selection_mode": "automatic",
        "automatic_count": 25,
        "allow_fewer": True,
        "times_json": ["12:00"],
        "duration_days": 1,
        "posts_per_day": 1,
    })
    assert allowed["planned_count"] == 20
    assert allowed["requested_count"] == 25
    assert allowed["can_approve"] is True


def test_no_media_blocks_approval(monkeypatch):
    from src.stories import autostory_operator_preview as mod

    monkeypatch.setattr(mod, "fleet_autostory_summary", lambda db: {
        "production_ready": 94, "available_today": 94, "eligible_now": 94,
        "at_daily_limit": 0, "temporarily_blocked": 0, "diagnostics": {}, "help": "",
        "_eligible_all_ids": list(range(1, 30)),
    })
    monkeypatch.setattr(mod, "select_automatic_accounts", lambda db, requested, exclude_ids=None: {
        "selection_mode": "automatic", "requested": "10", "requested_count": 10,
        "selected_account_ids": list(range(1, 11)), "selected_count": 10,
        "eligible_available": 94, "shortfall": 0, "production_ready": 94,
        "acknowledgment_required": False, "message": None,
    })
    monkeypatch.setattr(mod, "plan_waves_for_accounts", lambda db, ids: {
        "account_count": 10, "wave_count": 1,
        "waves": [{"index": 1, "size": 10, "account_ids": ids}],
        "max_wave_size": 25, "duplicates": 0, "any_over_max": False,
    })
    p = build_operator_campaign_preview(MagicMock(), {
        "selection_mode": "automatic", "automatic_count": 10,
        "times_json": ["12:00"], "duration_days": 1, "posts_per_day": 1,
    })
    assert p["media_ok"] is False
    assert p["can_approve"] is False


def test_multiday_awake_window_six_slots(monkeypatch):
    monkeypatch.setattr(
        "src.stories.autostory_operator_preview.operator_timezone_name",
        lambda: "Asia/Yerevan",
    )
    sched = normalize_times_json(None, posts_per_day=2)
    assert sched["local_times_json"] == ["10:00", "20:00"]
    times = sched["times_json"]
    assert times == ["06:00", "16:00"]
    rows = format_execution_times(
        times,
        start_at=datetime(2026, 8, 17, 8, 0, 0),
        duration_days=3,
        tz_name="Asia/Yerevan",
    )
    assert len(rows) == 6
    assert {r["local_time"] for r in rows} <= {"10:00", "20:00"}


def test_max_story_publishes_is_account_count_not_slots(monkeypatch):
    from src.stories import autostory_operator_preview as mod

    monkeypatch.setattr(mod, "fleet_autostory_summary", lambda db: {
        "production_ready": 94, "available_today": 94, "eligible_now": 94,
        "at_daily_limit": 0, "temporarily_blocked": 0, "diagnostics": {}, "help": "",
        "_eligible_all_ids": list(range(1, 95)),
    })
    monkeypatch.setattr(mod, "select_automatic_accounts", lambda db, requested, exclude_ids=None: {
        "selection_mode": "automatic", "requested": "25", "requested_count": 25,
        "selected_account_ids": list(range(1, 26)), "selected_count": 25,
        "eligible_available": 94, "shortfall": 0, "production_ready": 94,
        "acknowledgment_required": False, "message": None,
    })
    monkeypatch.setattr(mod, "plan_waves_for_accounts", lambda db, ids: {
        "account_count": 25, "wave_count": 1,
        "waves": [{"index": 1, "size": 25, "account_ids": ids}],
        "max_wave_size": 25, "duplicates": 0, "any_over_max": False,
    })
    # Legacy once path (explicit) — max equals planned accounts, not pickup slots
    p = build_operator_campaign_preview(MagicMock(), {
        "media_path": "/tmp/x.jpg",
        "selection_mode": "automatic",
        "automatic_count": 25,
        "duration_days": 3,
        "posts_per_day": 2,
        "campaign_mode": "accounts_publish_once",
    })
    assert p["planned_execution_count"] == 6
    assert p["max_story_publishes"] == 25
    assert p["campaign_semantics"]["model"] == "accounts_publish_once"
    assert "at most one" in p["campaign_semantics"]["explanation"].lower()
    assert "republish" in p["campaign_semantics"]["explanation"].lower()

    # New default recurring_daily — max = accounts × spad × days
    p2 = build_operator_campaign_preview(MagicMock(), {
        "media_path": "/tmp/x.jpg",
        "selection_mode": "automatic",
        "automatic_count": 25,
        "duration_days": 3,
        "campaign_mode": "recurring_daily",
        "stories_per_account_per_day": 1,
        "awake_start_hhmm": "10:00",
        "awake_end_hhmm": "20:00",
    })
    assert p2["max_story_publishes"] == 75
    assert p2["campaign_semantics"]["model"] == "recurring_daily"


def test_explicit_times_no_awake_in_preview():
    p = preview_schedule(duration_days=1, posts_per_day=1, times_json=["09:00", "13:30"])
    assert p["times_json"] == ["09:00", "13:30"]
    assert p.get("awake_window") in (None,)
    assert p.get("explicit_times") is True


def test_wave_plan_sixty(monkeypatch):
    from src.stories.autostory_hardening import plan_full_fleet_waves
    from src.stories import autostory_hardening as hard

    monkeypatch.setattr(hard, "rotate_account_ids", lambda _db, ids: list(ids))
    plan = plan_full_fleet_waves(MagicMock(), list(range(60)))
    assert [w["size"] for w in plan["waves"]] == [25, 25, 10]


def test_structured_log_events_wired():
    import inspect
    from src.stories import auto_story_service as svc
    from src.stories import autostory_hardening as hard
    src = inspect.getsource(svc.execute_wave) + inspect.getsource(svc._record_wave_progress_from_result)
    hard_src = inspect.getsource(hard.claim_campaign) + inspect.getsource(hard.revoke_wave_authorization)
    for event in (
        "autostory.campaign.started",
        "autostory.wave.started",
        "autostory.account.published",
        "autostory.account.failed",
        "autostory.wave.completed",
        "autostory.campaign.completed",
    ):
        assert event in src
    assert "autostory.campaign.claimed" in hard_src
    assert "autostory.authorization.revoked" in hard_src
    idle = inspect.getsource(svc.tick_due_auto_story_campaigns)
    assert "autostory.scheduler.idle" in idle
