"""GET /api/accounts/story-precheck-candidates returns deduped IDs for operator queues."""
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def flask_client():
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_precheck_candidates_dedupes_and_requires_admin_semantics(flask_client):
    acc1 = MagicMock()
    acc1.id = 10
    acc1.purpose = "both"
    acc1.status = MagicMock()
    acc1.status.value = "active"

    mock_db = MagicMock()
    mock_db.query.return_value.order_by.return_value.all.return_value = [acc1]

    @contextmanager
    def mock_get_db():
        yield mock_db

    def fake_avail(account, _canonical_exists=None):
        return {"story_ui_status": "needs_precheck"}

    with patch("src.dashboard.routes._admin_api_allowed", return_value=True):
        with patch("src.dashboard.routes.get_db_context", mock_get_db):
            with patch("src.core.session_paths.account_has_canonical_session", return_value=True):
                with patch(
                    "src.core.session_paths.get_existing_canonical_account_ids",
                    return_value=set(),
                ):
                    with patch(
                        "src.core.session_paths.get_story_availability",
                        side_effect=fake_avail,
                    ):
                        r = flask_client.get("/api/accounts/story-precheck-candidates?limit=50")

    assert r.status_code == 200
    data = r.get_json()
    assert data.get("success") is True
    assert data.get("account_ids") == [10]
    assert data.get("count") == 1
    assert data.get("nothing_to_run") is False
