"""
Tests for operations-readiness guardrails: canary mode, rate limits, bulk profile caps.
"""
import asyncio
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from src.stories.run_batch import run_batch_async
from src.core.risk_events import (
    count_events_last_hour,
    count_bulk_profile_actions_last_hour,
    EVENT_PRECHECK_RUN,
    EVENT_STORY_PUBLISHED,
    EVENT_USERNAME_CHANGED,
    EVENT_PROFILE_PHOTO_CHANGED,
)


def _run(coro):
    """Run async coroutine in sync context."""
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.mark.asyncio
async def test_run_batch_canary_caps_max_accounts():
    """Without canary_batch_ok, max_accounts is capped to canary_default_batch_size (1)."""
    eligible_one = [{"id": 1}]
    eligible_none = []

    with patch("src.stories.batch_helpers.get_story_eligible_accounts_for_batch") as mock_eligible:
        with patch("src.stories.run_batch.get_db_context"):
            with patch("src.stories.run_batch.StoryBatchRun") as MockRun:
                with patch("src.stories.publisher.story_publisher") as mock_pub:
                    mock_eligible.return_value = (eligible_one, [])

                    run_mock = MagicMock()
                    run_mock.id = 99
                    MockRun.return_value = run_mock

                    mock_pub.publish_batch = AsyncMock(return_value={
                        "total_attempted": 1,
                        "successful": 1,
                        "failed": 0,
                        "skipped": 0,
                        "errors": [],
                    })

                    result = await run_batch_async({
                        "media_path": "/tmp/x.jpg",
                        "caption": "",
                        "max_stories": 1,
                        "mentions_per_story": 0,
                        "canary_batch_ok": False,
                        "max_accounts": 10,
                    })

                    # Should have called with max_accounts=1 (canary cap)
                    call_kw = mock_eligible.call_args[1]
                    assert call_kw["max_accounts"] == 1
                    assert result.get("successful", 0) >= 1


@pytest.mark.asyncio
async def test_run_batch_canary_allows_more_with_opt_in():
    """With canary_batch_ok=true, max_accounts is not capped to 1."""
    eligible_two = [{"id": 1}, {"id": 2}]

    with patch("src.stories.batch_helpers.get_story_eligible_accounts_for_batch") as mock_eligible:
        with patch("src.stories.run_batch.get_db_context"):
            with patch("src.stories.run_batch.StoryBatchRun") as MockRun:
                with patch("src.stories.publisher.story_publisher") as mock_pub:
                    mock_eligible.return_value = (eligible_two, [])

                    run_mock = MagicMock()
                    run_mock.id = 99
                    MockRun.return_value = run_mock

                    mock_pub.publish_batch = AsyncMock(return_value={
                        "total_attempted": 2,
                        "successful": 2,
                        "failed": 0,
                        "skipped": 0,
                        "errors": [],
                    })

                    result = await run_batch_async({
                        "media_path": "/tmp/x.jpg",
                        "caption": "",
                        "max_stories": 2,
                        "mentions_per_story": 0,
                        "canary_batch_ok": True,
                        "max_accounts": 5,
                    })

                    call_kw = mock_eligible.call_args[1]
                    assert call_kw["max_accounts"] >= 2
                    assert result.get("successful", 0) >= 2


@pytest.mark.asyncio
async def test_run_batch_max_story_publishes_per_hour_blocks():
    """When recent publishes >= max_per_hour, batch is blocked before publish."""
    eligible_one = [{"id": 1}]

    with patch("src.stories.batch_helpers.get_story_eligible_accounts_for_batch") as mock_eligible:
        with patch("src.core.risk_events.count_events_last_hour") as mock_count:
            with patch("src.stories.run_batch.get_db_context"):
                with patch("src.stories.run_batch.StoryBatchRun") as MockRun:
                    mock_eligible.return_value = (eligible_one, [])
                    mock_count.return_value = 15

                    run_mock = MagicMock()
                    run_mock.id = 99
                    MockRun.return_value = run_mock

                    result = await run_batch_async({
                        "media_path": "/tmp/x.jpg",
                        "caption": "",
                        "max_stories": 1,
                        "mentions_per_story": 0,
                        "canary_batch_ok": True,
                    })

                    assert result.get("success") is False
                    assert "exceed story publish cap" in (result.get("error") or "")


def test_count_bulk_profile_actions_last_hour_combines_both():
    """count_bulk_profile_actions_last_hour sums username + photo events."""
    with patch("src.core.risk_events.count_events_last_hour") as mock:
        def side_effect(ev):
            if ev == EVENT_USERNAME_CHANGED:
                return 3
            if ev == EVENT_PROFILE_PHOTO_CHANGED:
                return 5
            return 0
        mock.side_effect = side_effect
        n = count_bulk_profile_actions_last_hour()
    assert n == 8


def test_bulk_profile_actions_cap_returns_429():
    """When count_bulk_profile_actions_last_hour >= max, bulk-set-username returns 429."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with patch("src.dashboard.routes._admin_api_allowed", return_value=True):
        with patch("src.core.risk_events.count_bulk_profile_actions_last_hour", return_value=8):
            r = client.post(
                "/api/accounts/bulk-set-username",
                json={"username_prefix": "test"},
                content_type="application/json",
            )
    assert r.status_code == 429
    data = r.get_json()
    assert "cap" in (data.get("error") or "").lower() or "bulk" in (data.get("error") or "").lower()


def test_max_prechecks_per_hour_enforced():
    """When count_events_last_hour(PRECHECK_RUN) >= max, precheck route returns 429."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with patch("src.dashboard.routes._admin_api_allowed", return_value=True):
        with patch("src.core.risk_events.count_events_last_hour", return_value=20):
            with patch("src.core.session_paths.account_has_canonical_session", return_value=True):
                r = client.post(
                    "/api/accounts/story-precheck",
                    json={"account_ids": [1]},
                    content_type="application/json",
                )
                assert r.status_code == 429, f"Expected 429, got {r.status_code}"
                data = r.get_json()
                assert "precheck" in (data.get("error") or "").lower() or "cap" in (data.get("error") or "").lower() or "limit" in (data.get("error") or "").lower()


def test_bulk_profile_actions_cap_returns_429():
    """When count_bulk_profile_actions_last_hour >= max, bulk username returns 429."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with patch("src.dashboard.routes._admin_api_allowed", return_value=True):
        with patch("src.core.risk_events.count_bulk_profile_actions_last_hour", return_value=8):
            r = client.post(
                "/api/accounts/bulk-set-username",
                json={"username_prefix": "test"},
                content_type="application/json",
            )
    assert r.status_code == 429
    data = r.get_json()
    assert "cap" in (data.get("error") or "").lower() or "limit" in (data.get("error") or "").lower()


def test_precheck_canary_blocks_more_than_one_without_opt_in():
    """Precheck with >1 accounts and no canary_batch_ok returns 400."""
    from src.dashboard.app import create_app
    from contextlib import contextmanager

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    # Mock 2 accounts so canary check triggers (no real DB needed)
    acc1 = MagicMock()
    acc1.id = 101
    acc2 = MagicMock()
    acc2.id = 102

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.all.return_value = [acc1, acc2]

    @contextmanager
    def mock_get_db():
        yield mock_db

    with patch("src.dashboard.routes._admin_api_allowed", return_value=True):
        with patch("src.core.session_paths.account_has_canonical_session", return_value=True):
            with patch("src.core.risk_events.count_events_last_hour", return_value=0):
                with patch("src.dashboard.routes.get_db_context", mock_get_db):
                    r = client.post(
                        "/api/accounts/story-precheck",
                        json={"account_ids": [101, 102], "canary_batch_ok": False},
                        content_type="application/json",
                    )
    assert r.status_code == 400
    data = r.get_json()
    assert data
    assert "canary" in (data.get("error") or "").lower()


def test_bulk_profile_actions_cap_returns_429():
    """When count_bulk_profile_actions_last_hour >= max, bulk username returns 429."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with patch("src.dashboard.routes._admin_api_allowed", return_value=True):
        with patch("src.core.risk_events.count_bulk_profile_actions_last_hour", return_value=8):
            r = client.post(
                "/api/accounts/bulk-set-username",
                json={"username_prefix": "testbrand"},
                content_type="application/json",
            )
    assert r.status_code == 429
    data = r.get_json()
    assert "bulk profile" in (data.get("error") or "").lower() or "cap" in (data.get("error") or "").lower()


def test_bulk_profile_actions_cap_returns_429():
    """When count_bulk_profile_actions_last_hour >= max, bulk username/photo return 429."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with patch("src.dashboard.routes._admin_api_allowed", return_value=True):
        with patch("src.core.risk_events.count_bulk_profile_actions_last_hour", return_value=8):
            r = client.post(
                "/api/accounts/bulk-set-username",
                json={"username_prefix": "test"},
                content_type="application/json",
            )
    assert r.status_code == 429
    data = r.get_json()
    assert "cap" in (data.get("error") or "").lower() or "retry" in (data.get("error") or "").lower()


def test_bulk_profile_actions_cap_returns_429():
    """When count_bulk_profile_actions_last_hour >= max, bulk username route returns 429."""
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with patch("src.dashboard.routes._admin_api_allowed", return_value=True):
        with patch("src.core.risk_events.count_bulk_profile_actions_last_hour", return_value=8):
            r = client.post(
                "/api/accounts/bulk-set-username",
                json={"username_prefix": "test"},
                content_type="application/json",
            )
    # Cap is 8 by default; 8 >= 8 triggers 429
    assert r.status_code == 429
    data = r.get_json()
    assert "bulk profile" in (data.get("error") or "").lower() or "cap" in (data.get("error") or "").lower()
