"""story_from_db enrichment: read-only DB snapshot; health engine stays connect/auth/get_me only."""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest


@contextmanager
def _fake_db_context(accounts):
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.all.return_value = accounts
    yield mock_db


def _dec(reason_code: str, human_reason: str = ""):
    d = MagicMock()
    d.reason_code = reason_code
    d.human_reason = human_reason or reason_code
    return d


def test_snapshot_alive_story_frozen_state():
    from src.dashboard.routes import _enrich_health_results_with_db_story_state

    acc = MagicMock()
    acc.id = 93
    acc.story_blocked_until = None
    results = [{"account_id": 93, "status": "alive", "session_valid": True}]
    sa = {
        "story_ui_status": "frozen",
        "story_available_label": "Unknown",
        "story_reason": "Stories blocked",
        "is_story_ready": False,
        "story_precheck_stale": True,
    }

    with patch("src.dashboard.routes.get_db_context", lambda: _fake_db_context([acc])):
        with patch("src.core.session_paths.get_story_availability", return_value=sa):
            with patch("src.core.safety_policy.get_story_safety_decision", return_value=_dec("story_frozen")):
                _enrich_health_results_with_db_story_state(results)

    sf = results[0]["story_from_db"]
    assert sf is not None
    assert sf["state"] == "frozen"
    assert sf["safety_reason"] == "story_frozen"


def test_snapshot_alive_stale_precheck_needs_precheck():
    from src.dashboard.routes import _enrich_health_results_with_db_story_state

    acc = MagicMock()
    acc.id = 12
    acc.story_blocked_until = None
    results = [{"account_id": 12, "status": "alive", "session_valid": True}]
    sa = {
        "story_ui_status": "needs_precheck",
        "story_available_label": "Not checked",
        "story_reason": "Precheck required",
        "is_story_ready": False,
        "story_precheck_stale": True,
    }

    with patch("src.dashboard.routes.get_db_context", lambda: _fake_db_context([acc])):
        with patch("src.core.session_paths.get_story_availability", return_value=sa):
            with patch("src.core.safety_policy.get_story_safety_decision", return_value=_dec("daily_cap")):
                _enrich_health_results_with_db_story_state(results)

    assert results[0]["story_from_db"]["state"] == "needs_precheck"


def test_snapshot_alive_rate_limited():
    from src.dashboard.routes import _enrich_health_results_with_db_story_state

    acc = MagicMock()
    acc.id = 5
    acc.story_blocked_until = None
    results = [{"account_id": 5, "status": "alive", "session_valid": True}]
    sa = {
        "story_ui_status": "rate_limited",
        "story_available_label": "Tomorrow",
        "story_reason": "cooldown",
        "is_story_ready": False,
        "story_precheck_stale": False,
    }

    with patch("src.dashboard.routes.get_db_context", lambda: _fake_db_context([acc])):
        with patch("src.core.session_paths.get_story_availability", return_value=sa):
            with patch("src.core.safety_policy.get_story_safety_decision", return_value=_dec("story_rate_limited")):
                _enrich_health_results_with_db_story_state(results)

    assert results[0]["story_from_db"]["state"] == "rate_limited"


def test_snapshot_alive_warmup_hold():
    from src.dashboard.routes import _enrich_health_results_with_db_story_state

    acc = MagicMock()
    acc.id = 7
    acc.story_blocked_until = None
    results = [{"account_id": 7, "status": "alive", "session_valid": True}]
    sa = {
        "story_ui_status": "warmup_hold",
        "story_available_label": "Warmup Hold",
        "story_reason": "warming",
        "is_story_ready": False,
        "story_precheck_stale": False,
    }

    with patch("src.dashboard.routes.get_db_context", lambda: _fake_db_context([acc])):
        with patch("src.core.session_paths.get_story_availability", return_value=sa):
            with patch("src.core.safety_policy.get_story_safety_decision", return_value=_dec("warmup_pending")):
                _enrich_health_results_with_db_story_state(results)

    assert results[0]["story_from_db"]["state"] == "warmup_hold"


def test_story_db_snapshot_helper_keys():
    from src.dashboard.routes import _story_db_snapshot_from_account

    acc = MagicMock()
    acc.story_blocked_until = None
    sa = {
        "story_ui_status": "ready",
        "story_available_label": "Now",
        "story_reason": "ok",
        "is_story_ready": True,
        "story_precheck_stale": False,
    }
    with patch("src.core.session_paths.get_story_availability", return_value=sa):
        with patch("src.core.safety_policy.get_story_safety_decision", return_value=_dec("daily_cap")):
            snap = _story_db_snapshot_from_account(acc)

    assert set(snap.keys()) == {"state", "label", "reason", "blocked_until", "precheck_stale", "safety_reason"}
    assert snap["state"] == "ready"
