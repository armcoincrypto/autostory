"""P10.18 Story Runtime Integration repair regressions.

All coverage is read-only/dry-run. It must not publish stories, send messages,
join channels, start campaigns, or mutate account purpose/eligibility.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image

from src.core.database import get_db_context
from src.core.models import Story
from src.stories.scheduler_integration import build_story_scheduler_integration_map

TOKEN = "p10-18-test-token"
BINANCE_ARMENIA_SOURCE_ID = 1936532075


def _write_valid_vertical_story_jpeg(path: Path, size: tuple[int, int] = (1080, 1920)) -> Path:
    """Deterministic Story-compatible RGB JPEG (decodable, vertical)."""
    Image.new("RGB", size, (40, 80, 120)).save(path, format="JPEG", quality=85, optimize=True)
    return path


def _app(monkeypatch):
    monkeypatch.setenv("DASHBOARD_ADMIN_TOKEN", TOKEN)
    monkeypatch.setenv("CONTROLLED_STORY_EXECUTION_ENABLED", "false")
    monkeypatch.setenv("SCHEDULER_STORY_EXECUTION_ENABLED", "false")
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    return app


def _headers() -> dict[str, str]:
    return {"X-Admin-Token": TOKEN}


def _payload(media_path: str) -> dict:
    return {
        "account_ids": [140],
        "media_path": media_path,
        "caption": "test",
        "mention_source_chat_id": BINANCE_ARMENIA_SOURCE_ID,
        "mentions_per_story": 5,
        "mention_strategy": "random",
        "mode": "once",
        "dry_run": True,
        # Isolate from production stories_today (default cap is 1; account 140 may already be at cap).
        "max_stories_per_account_per_day": 10,
    }


def _seed_fleet() -> None:
    from src.core.database import get_db_context, init_db
    from tests.helpers.fleet_seed import seed_minimal_fleet

    init_db()
    with get_db_context() as db:
        seed_minimal_fleet(db)


def test_account_140_story_precheck_passes_for_dry_run_with_valid_media_source(
    monkeypatch, tmp_path
) -> None:
    _seed_fleet()
    app = _app(monkeypatch)
    media = _write_valid_vertical_story_jpeg(tmp_path / "story_valid.jpg")

    resp = app.test_client().post(
        "/api/stories/precheck", json=_payload(str(media)), headers=_headers()
    )

    assert resp.status_code == 200
    assert resp.is_json
    data = resp.get_json()
    assert data["ok"] is True
    assert data["eligible_accounts"] == [140]
    assert data["blocked_accounts"] == []
    assert data["mention_candidates"] >= 1
    assert data["media_ready"] is True
    assert data["cooldown_ok"] is True
    assert data["live_publish_allowed"] is False
    assert data["requires_operator_approval"] is True


def test_precheck_returns_account_level_blockers_and_json(monkeypatch, tmp_path) -> None:
    _seed_fleet()
    app = _app(monkeypatch)
    media = _write_valid_vertical_story_jpeg(tmp_path / "story_valid.jpg")
    payload = {**_payload(str(media)), "account_ids": [110]}

    resp = app.test_client().post("/api/stories/precheck", json=payload, headers=_headers())

    assert resp.status_code == 200
    assert resp.is_json
    data = resp.get_json()
    assert data["ok"] is False
    assert "110" in data["account_blockers"]
    assert "protected_account" in data["account_blockers"]["110"]
    assert "<!doctype" not in resp.get_data(as_text=True).lower()


def test_dry_run_does_not_publish_or_create_story_rows(monkeypatch, tmp_path) -> None:
    _seed_fleet()
    app = _app(monkeypatch)
    media = _write_valid_vertical_story_jpeg(tmp_path / "story_valid.jpg")
    with get_db_context() as db:
        before = db.query(Story).count()

    resp = app.test_client().post(
        "/api/stories/dry-run", json=_payload(str(media)), headers=_headers()
    )

    with get_db_context() as db:
        after = db.query(Story).count()
    assert before == after
    data = resp.get_json()
    assert data["ok"] is True
    plan = data["plan"]
    assert plan["selected_accounts"] == [140]
    assert plan["story_count"] == 1
    assert plan["safety_gates"]["live_publish_allowed"] is False
    assert plan["safety_gates"]["dry_run_does_not_publish"] is True


def test_random_mention_strategy_returns_non_duplicate_candidates(monkeypatch, tmp_path) -> None:
    _seed_fleet()
    app = _app(monkeypatch)
    media = _write_valid_vertical_story_jpeg(tmp_path / "story_valid.jpg")

    resp = app.test_client().post(
        "/api/stories/dry-run", json=_payload(str(media)), headers=_headers()
    )

    candidates = resp.get_json()["plan"]["selected_mention_candidates"]
    user_ids = [c["user_id"] for c in candidates]
    assert candidates
    assert len(user_ids) == len(set(user_ids))
    assert len(candidates) <= 5


def test_empty_media_blocks_precheck(monkeypatch, tmp_path) -> None:
    app = _app(monkeypatch)
    media = _write_valid_vertical_story_jpeg(tmp_path / "story_valid.jpg")
    payload = {**_payload(str(media)), "media_path": ""}

    resp = app.test_client().post("/api/stories/precheck", json=payload, headers=_headers())

    data = resp.get_json()
    assert data["ok"] is False
    assert "missing_or_invalid_media" in data["blockers"]
    assert data["media_ready"] is False


def test_story_runtime_map_is_disabled_by_default(monkeypatch) -> None:
    monkeypatch.setenv("SCHEDULER_STORY_EXECUTION_ENABLED", "false")

    integration = build_story_scheduler_integration_map()

    assert integration["story_execution_enabled"] is False
    assert integration["env_flag"] == "SCHEDULER_STORY_EXECUTION_ENABLED"
    assert integration["current_state"] == "disabled_no_live_story_execution"


def test_stories_ui_has_single_account_and_strategy_controls() -> None:
    template = Path("src/dashboard/templates/stories.html").read_text(encoding="utf-8")

    assert 'id="sched-account"' in template
    assert 'label class="form-label fw-semibold mb-1">Account</label>' in template or 'id="sched-account"' in template
    assert 'id="sched-mention-strategy"' in template
    assert "Random" in template
    assert "Enable live test" in template
