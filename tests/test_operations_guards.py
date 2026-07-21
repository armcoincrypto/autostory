"""Tests for current operations-readiness route and risk-event guardrails."""
from unittest.mock import patch, MagicMock

from src.core.risk_events import (
    count_events_last_hour,
    count_bulk_profile_actions_last_hour,
    EVENT_PRECHECK_RUN,
    EVENT_STORY_PUBLISHED,
    EVENT_USERNAME_CHANGED,
    EVENT_PROFILE_PHOTO_CHANGED,
)


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
