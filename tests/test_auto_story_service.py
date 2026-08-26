"""Auto Story service: schedule spread, next wave, gates, mention non-reuse."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from src.stories.auto_story_service import (
    _run_coro_sync,
    compute_daily_times,
    next_wave_after,
    preview_schedule,
    tick_due_auto_story_campaigns,
)
from src.stories.rotation_audit import (
    allocate_mentions_without_replacement,
    effective_live_mutation_allowlist_account_ids,
)
from src.stories.scheduler_integration import build_story_scheduler_integration_map


def test_compute_daily_times_spread() -> None:
    assert compute_daily_times(1) == ["15:00"]
    assert compute_daily_times(2) == ["10:00", "20:00"]
    assert compute_daily_times(3) == ["10:00", "15:00", "20:00"]
    t4 = compute_daily_times(4)
    assert t4[0] == "10:00"
    assert t4[-1] == "20:00"
    assert len(t4) == 4


def test_next_wave_after_same_day() -> None:
    now = datetime(2026, 7, 28, 9, 0, 0)
    ends = now + timedelta(days=3)
    nxt = next_wave_after(now=now, times=["10:00", "15:00", "20:00"], ends_at=ends)
    assert nxt == datetime(2026, 7, 28, 10, 0, 0)


def test_next_wave_after_rolls_next_day() -> None:
    now = datetime(2026, 7, 28, 20, 30, 0)
    ends = now + timedelta(days=2)
    nxt = next_wave_after(now=now, times=["10:00", "20:00"], ends_at=ends)
    assert nxt == datetime(2026, 7, 29, 10, 0, 0)


def test_next_wave_none_past_end() -> None:
    now = datetime(2026, 7, 28, 12, 0, 0)
    ends = datetime(2026, 7, 28, 11, 0, 0)
    assert next_wave_after(now=now, times=["10:00", "15:00"], ends_at=ends) is None


def test_preview_schedule_shape(monkeypatch) -> None:
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
    assert p["duration_days"] == 5
    assert p.get("awake_window") == "10:00-20:00"
    assert p.get("schedule_mode") == "awake_window"
    assert p.get("explicit_times") is False


def test_preview_explicit_times_json() -> None:
    p = preview_schedule(duration_days=1, posts_per_day=1, times_json=["09:00", "13:30"])
    assert p["times_json"] == ["09:00", "13:30"]
    assert p.get("explicit_times") is True
    assert p.get("awake_window") is None


def test_ten_accounts_unique_mentions() -> None:
    accounts = list(range(101, 111))
    candidates = [{"user_id": i, "username": f"u{i}"} for i in range(1, 11)]
    allocated = allocate_mentions_without_replacement(
        candidates,
        account_ids=accounts,
        mentions_per_story=1,
    )
    assert len(allocated) == 10
    flat = [c["user_id"] for chunk in allocated for c in chunk]
    assert flat == list(range(1, 11))
    assert len(flat) == len(set(flat))


def test_effective_allowlist_empty_when_mutations_off(monkeypatch) -> None:
    monkeypatch.setenv("STORY_MUTATIONS_ENABLED", "false")
    monkeypatch.setenv("CONTROLLED_STORY_EXECUTION_ENABLED", "false")
    monkeypatch.delenv("STORY_ACCOUNT_MUTATION_ALLOWLIST", raising=False)
    assert effective_live_mutation_allowlist_account_ids() == set()


def test_effective_allowlist_when_live(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("STORY_MUTATIONS_ENABLED", "true")
    monkeypatch.setenv("CONTROLLED_STORY_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("STORY_ACCOUNT_MUTATION_ALLOWLIST", "106,140,108")
    assert effective_live_mutation_allowlist_account_ids() == {106, 140, 108}


def test_scheduler_tick_skipped_when_disabled(monkeypatch) -> None:
    monkeypatch.setenv("SCHEDULER_STORY_EXECUTION_ENABLED", "false")
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("STORY_MUTATIONS_ENABLED", "true")
    out = tick_due_auto_story_campaigns()
    assert out["skipped"] is True
    assert out["reason"] == "scheduler_story_execution_disabled"


def test_scheduler_integration_map_engine() -> None:
    m = build_story_scheduler_integration_map()
    assert m["engine"] == "auto_story_campaigns"


def test_run_coro_sync_without_running_loop() -> None:
    async def _one() -> int:
        return 1

    assert _run_coro_sync(_one()) == 1


def test_run_coro_sync_inside_running_loop() -> None:
    async def _inner() -> str:
        async def _work() -> str:
            await asyncio.sleep(0)
            return "ok"

        return _run_coro_sync(_work())

    assert asyncio.run(_inner()) == "ok"
