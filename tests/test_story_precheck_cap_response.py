"""Batch story-precheck HTTP responses when hourly cap blocks the request (no Telegram work)."""
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def flask_client():
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_precheck_hard_cap_includes_zero_processed(flask_client):
    with patch("src.dashboard.routes._admin_api_allowed", return_value=True):
        with patch("src.core.risk_events.count_events_last_hour", return_value=20):
            with patch("src.core.session_paths.account_has_canonical_session", return_value=True):
                r = flask_client.post(
                    "/api/accounts/story-precheck",
                    json={"account_ids": [1]},
                    content_type="application/json",
                )
    assert r.status_code == 429
    data = r.get_json()
    assert data.get("success") is False
    assert data.get("processed_count") == 0
    assert data.get("nothing_processed") is True
    assert data.get("remaining_capacity") == 0


def test_precheck_would_exceed_cap_includes_remaining_capacity(flask_client):
    accs = [MagicMock(id=i) for i in (101, 102, 103, 104, 105)]
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.all.return_value = accs

    @contextmanager
    def mock_get_db():
        yield mock_db

    with patch("src.dashboard.routes._admin_api_allowed", return_value=True):
        with patch("src.core.session_paths.account_has_canonical_session", return_value=True):
            with patch("src.core.risk_events.count_events_last_hour", return_value=18):
                with patch("src.dashboard.routes.get_db_context", mock_get_db):
                    with patch("src.dashboard.routes.run_async_with_timeout") as mock_run:
                        r = flask_client.post(
                            "/api/accounts/story-precheck",
                            json={"account_ids": [101, 102, 103, 104, 105], "canary_batch_ok": True},
                            content_type="application/json",
                            )
    assert r.status_code == 429
    data = r.get_json()
    assert data.get("processed_count") == 0
    assert data.get("nothing_processed") is True
    assert data.get("remaining_capacity") == 2
    assert "would exceed" in (data.get("error") or "").lower()
    mock_run.assert_not_called()
