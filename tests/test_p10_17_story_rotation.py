"""P10.17 Story Rotation audit/UI regressions.

These tests are read-only: no Telegram publish, send, join, scheduler execution,
campaign enablement, or account-purpose mutation is performed.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from src.stories.rotation_audit import classify_story_run, story_purpose_compatible

TOKEN = "p10-17-test-token"


def _app(monkeypatch):
    monkeypatch.setenv("DASHBOARD_ADMIN_TOKEN", TOKEN)
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    return app


def _auth_headers() -> dict[str, str]:
    return {"X-Admin-Token": TOKEN}


def test_purpose_both_counts_as_story_compatible() -> None:
    assert story_purpose_compatible("both") is True
    assert story_purpose_compatible("stories") is True
    assert story_purpose_compatible("autostory") is True
    assert story_purpose_compatible("disabled") is False
    assert story_purpose_compatible("ai_agent") is False


def test_stale_and_pending_empty_runs_are_classified() -> None:
    now = datetime(2026, 5, 26, 12, 0, 0)
    stale = SimpleNamespace(
        id=1,
        status="running",
        stories_ok=0,
        stories_failed=0,
        started_at=now - timedelta(hours=3),
        last_tick_at=now - timedelta(hours=2),
        created_at=now - timedelta(hours=3),
        media_path="/tmp/story.jpg",
        pool_id=None,
        mode="once",
    )
    empty = SimpleNamespace(
        id=4,
        status="pending",
        stories_ok=0,
        stories_failed=0,
        started_at=None,
        last_tick_at=None,
        created_at=now,
        media_path="/tmp/story.jpg",
        pool_id=None,
        mode="once",
    )

    assert classify_story_run(stale, now=now)["hygiene_action"] == "FAILED_STALE"
    assert classify_story_run(empty, now=now)["hygiene_action"] == "SKIPPED_EMPTY"


def test_story_precheck_endpoint_returns_json_and_blocks_missing_media(monkeypatch) -> None:
    app = _app(monkeypatch)

    resp = app.test_client().post(
        "/api/stories/precheck",
        json={"mentions_per_story": 1},
        headers=_auth_headers(),
    )

    assert resp.status_code == 200
    assert resp.is_json
    payload = resp.get_json()
    precheck = payload["precheck"]
    assert payload["ok"] is False
    assert precheck["media"]["ok"] is False
    assert "missing_or_invalid_media" in precheck["live_blockers"]
    assert precheck["settings"]["all_ready_accounts_mode"] is True
    assert precheck["settings"]["pool_optional"] is True


def test_empty_mention_source_shows_warning(monkeypatch) -> None:
    app = _app(monkeypatch)

    resp = app.test_client().post(
        "/api/stories/precheck",
        json={"mentions_per_story": 1, "mention_source_chat_id": -999999999},
        headers=_auth_headers(),
    )

    precheck = resp.get_json()["precheck"]
    assert precheck["mentions"]["available"] == 0
    assert precheck["mentions"]["warning"] == "No discovered users available for selected mention source."


def test_protected_and_reserved_accounts_are_not_story_ready(monkeypatch) -> None:
    from src.core.database import get_db_context, init_db
    from tests.helpers.fleet_seed import seed_minimal_fleet

    init_db()
    with get_db_context() as db:
        seed_minimal_fleet(db)

    app = _app(monkeypatch)

    resp = app.test_client().get("/api/accounts?limit=500", headers=_auth_headers())

    assert resp.status_code == 200
    assert resp.is_json
    accounts = resp.get_json()
    protected_or_reserved = [
        row for row in accounts if row.get("protected_or_held") or row.get("ai_agent_reserved")
    ]
    assert protected_or_reserved
    assert all(row["publish_story_status"] == "no" for row in protected_or_reserved)


def test_story_eligible_accounts_api_returns_json_not_html(monkeypatch) -> None:
    app = _app(monkeypatch)

    resp = app.test_client().get("/api/stories/eligible-accounts", headers=_auth_headers())

    assert resp.status_code == 200
    assert resp.is_json
    payload = resp.get_json()
    assert payload["ok"] is True
    assert "counts" in payload
    assert "<!doctype" not in resp.get_data(as_text=True).lower()

# test_story_ui_exposes_precheck_dryrun_and_locked_live_button removed:
# asserted markup from a prior Story Ops Console UI iteration (standalone
# "Precheck"/"Start approved Story" wizard) that no longer exists in
# src/dashboard/templates/stories.html -- 6 of its 8 checked strings have
# zero occurrences in the current template, which uses a different
# multi-account selection UI. See also test_p10_23_stories_ops_ux.py
# (same obsolete UI generation, removed wholesale).
