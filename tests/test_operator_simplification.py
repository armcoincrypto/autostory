"""Operator simplification: media classification + preview contract smoke."""
from __future__ import annotations

from src.dashboard.routes import _is_system_test_media


def test_system_test_media_classification() -> None:
    assert _is_system_test_media("canary_107_story_1080x1920.jpg") is True
    assert _is_system_test_media("audit_test.jpg") is True
    assert _is_system_test_media("scheduled_production_check_1080x1920.jpg") is True
    assert _is_system_test_media("test.story.jpg") is True
    assert _is_system_test_media("Screenshot_2026-07-21.png") is False
    assert _is_system_test_media("campaign_hero.jpg") is False


def test_preview_mentions_default_and_max_publishes() -> None:
    from unittest.mock import MagicMock
    from src.stories import autostory_operator_preview as mod

    mod.fleet_autostory_summary = lambda db: {
        "production_ready": 94,
        "available_today": 94,
        "eligible_now": 94,
        "at_daily_limit": 0,
        "temporarily_blocked": 0,
        "diagnostics": {},
        "help": "",
        "_eligible_all_ids": list(range(1, 95)),
    }
    mod.select_automatic_accounts = lambda db, requested, exclude_ids=None: {
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
    }
    mod.plan_waves_for_accounts = lambda db, ids: {
        "account_count": 25,
        "wave_count": 1,
        "waves": [{"index": 1, "size": 25, "account_ids": ids}],
        "max_wave_size": 25,
        "duplicates": 0,
        "any_over_max": False,
    }
    p = mod.build_operator_campaign_preview(
        MagicMock(),
        {
            "media_path": "/tmp/x.jpg",
            "selection_mode": "automatic",
            "automatic_count": 25,
            "duration_days": 2,
            "posts_per_day": 1,
        },
    )
    assert p["mentions_per_story"] == 0
    assert p["max_story_publishes"] == 25
    assert p["planned_execution_count"] == 2
    assert p["campaign_semantics"]["model"] == "accounts_publish_once"
