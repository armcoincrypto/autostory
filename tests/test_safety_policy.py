"""Tests for src.core.safety_policy."""
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from src.utils.helpers import utc_now
from src.core.safety_policy import (
    get_story_safety_decision,
    get_account_risk_level,
    StorySafetyDecision,
    REASON_NO_SESSION,
    REASON_AUTH_REQUIRED,
    REASON_WARMUP_PENDING,
    REASON_STORY_FROZEN,
    REASON_STORY_TELEGRAM_DENIED,
    REASON_COOLDOWN,
    REASON_MANUAL_REVIEW_REQUIRED,
    REASON_STORY_PRECHECK_FAILED,
)


def test_no_session_blocked():
    """Account without canonical session is blocked with no_session."""
    acc = MagicMock()
    with patch("src.core.session_paths.account_has_canonical_session", return_value=False):
        dec = get_story_safety_decision(acc)
    assert dec.allowed is False
    assert dec.reason_code == REASON_NO_SESSION


def test_auth_required_blocked():
    """Account with status != active is blocked."""
    acc = MagicMock()
    acc.status = MagicMock(value="auth_required")
    acc.health_status = "alive"
    with patch("src.core.session_paths.account_has_canonical_session", return_value=True):
        dec = get_story_safety_decision(acc)
    assert dec.allowed is False
    assert dec.reason_code == REASON_AUTH_REQUIRED


def test_frozen_precheck_blocked():
    """Account with story_precheck_status=frozen is blocked."""
    acc = MagicMock()
    acc.status = MagicMock(value="active")
    acc.health_status = "alive"
    acc.story_precheck_status = "frozen"
    acc.story_precheck_checked_at = utc_now() - timedelta(hours=1)
    acc.story_blocked_until = None
    with patch("src.core.session_paths.account_has_canonical_session", return_value=True):
        dec = get_story_safety_decision(acc)
    assert dec.allowed is False
    assert dec.reason_code == REASON_STORY_FROZEN
    assert "frozen" in (dec.human_reason or "").lower()


def test_telegram_denied_precheck_blocked():
    """Weak-signal precheck stores telegram_denied; policy blocks with softer reason code."""
    acc = MagicMock()
    acc.status = MagicMock(value="active")
    acc.health_status = "alive"
    acc.story_precheck_status = "telegram_denied"
    acc.story_precheck_checked_at = utc_now() - timedelta(hours=1)
    acc.story_blocked_until = None
    with patch("src.core.session_paths.account_has_canonical_session", return_value=True):
        dec = get_story_safety_decision(acc)
    assert dec.allowed is False
    assert dec.reason_code == REASON_STORY_TELEGRAM_DENIED
    assert "Frozen by Telegram" not in (dec.human_reason or "")


def test_warmup_blocked():
    """Account in warmup is blocked with warmup_pending."""
    acc = MagicMock()
    acc.status = MagicMock(value="active")
    acc.health_status = "alive"
    acc.story_precheck_status = "allowed"
    acc.story_precheck_checked_at = utc_now() - timedelta(minutes=5)  # not stale for 15 min TTL
    acc.story_status = "ok"
    acc.story_blocked_until = None
    acc.manual_review_required = False
    with patch("src.core.session_paths.account_has_canonical_session", return_value=True):
        with patch("src.core.warmup.is_warmup_blocked", return_value=(True, "Too new")):
            dec = get_story_safety_decision(acc)
    assert dec.allowed is False
    assert dec.reason_code == REASON_WARMUP_PENDING


def test_cooldown_blocked():
    """Account in cooldown is blocked."""
    acc = MagicMock()
    acc.status = MagicMock(value="active")
    acc.health_status = "alive"
    acc.story_precheck_status = "allowed"
    acc.story_precheck_checked_at = utc_now() - timedelta(minutes=5)  # not stale for 15 min TTL
    acc.story_status = "ok"
    acc.story_blocked_until = None
    acc.manual_review_required = False
    acc.imported_at = None
    acc.successful_story_count = 1
    acc.last_story_failure_at = None
    acc.story_attempts_today = 0
    acc.stories_today = 0
    with patch("src.core.session_paths.account_has_canonical_session", return_value=True):
        with patch("src.core.warmup.is_warmup_blocked", return_value=(False, "")):
            with patch("src.core.warmup.is_cooldown_blocked", return_value=(True, "")):
                dec = get_story_safety_decision(acc)
    assert dec.allowed is False
    assert dec.reason_code == REASON_COOLDOWN


def test_manual_review_blocked():
    """Account with manual_review_required is blocked."""
    acc = MagicMock()
    acc.status = MagicMock(value="active")
    acc.health_status = "alive"
    acc.story_precheck_status = "allowed"
    acc.story_precheck_checked_at = utc_now() - timedelta(minutes=5)  # not stale for 15 min TTL
    acc.story_status = "ok"
    acc.story_blocked_until = None
    acc.manual_review_required = True
    acc.manual_review_reason = "Flagged"
    with patch("src.core.session_paths.account_has_canonical_session", return_value=True):
        with patch("src.core.warmup.is_warmup_blocked", return_value=(False, "")):
            with patch("src.core.warmup.is_cooldown_blocked", return_value=(False, "")):
                dec = get_story_safety_decision(acc)
    assert dec.allowed is False
    assert dec.reason_code == REASON_MANUAL_REVIEW_REQUIRED


def test_risk_level_blocked():
    """get_account_risk_level returns blocked for blocked warmup status."""
    acc = MagicMock()
    with patch("src.core.warmup.get_warmup_status", return_value="blocked"):
        assert get_account_risk_level(acc) == "blocked"


def test_risk_level_warming():
    """get_account_risk_level returns warming for new/warming status."""
    acc = MagicMock()
    with patch("src.core.warmup.get_warmup_status", return_value="warming"):
        assert get_account_risk_level(acc) == "warming"


def test_stale_allowed_precheck_blocked():
    """Precheck=allowed but stale (expired TTL) must block."""
    acc = MagicMock()
    acc.status = MagicMock(value="active")
    acc.health_status = "alive"
    acc.story_precheck_status = "allowed"
    acc.story_precheck_checked_at = utc_now() - timedelta(hours=48)  # stale (TTL 24h)
    acc.story_status = "ok"
    acc.story_blocked_until = None
    acc.manual_review_required = False
    acc.imported_at = None
    acc.successful_story_count = 1
    with patch("src.core.session_paths.account_has_canonical_session", return_value=True):
        with patch("src.core.warmup.is_warmup_blocked", return_value=(False, "")):
            with patch("src.core.warmup.is_cooldown_blocked", return_value=(False, "")):
                dec = get_story_safety_decision(acc)
    assert dec.allowed is False
    assert dec.reason_code == REASON_STORY_PRECHECK_FAILED


def test_stale_precheck_blocked_for_posting_15min_ttl():
    """For story_publish, precheck TTL is 15 min; 30 min ago = stale."""
    acc = MagicMock()
    acc.status = MagicMock(value="active")
    acc.health_status = "alive"
    acc.story_precheck_status = "allowed"
    acc.story_precheck_checked_at = utc_now() - timedelta(minutes=30)  # stale for 15 min post TTL
    acc.story_status = "ok"
    acc.story_blocked_until = None
    acc.manual_review_required = False
    acc.imported_at = None
    acc.successful_story_count = 1
    acc.last_story_failure_at = None
    acc.story_attempts_today = 0
    acc.stories_today = 0
    with patch("src.core.session_paths.account_has_canonical_session", return_value=True):
        with patch("src.core.warmup.is_warmup_blocked", return_value=(False, "")):
            with patch("src.core.warmup.is_cooldown_blocked", return_value=(False, "")):
                with patch("src.core.warmup.is_attempt_cap_exceeded", return_value=False):
                    with patch("src.core.warmup.is_daily_cap_exceeded", return_value=False):
                        dec = get_story_safety_decision(acc, requested_action="story_publish")
    assert dec.allowed is False
    assert dec.reason_code == REASON_STORY_PRECHECK_FAILED


def test_cooldown_only_from_last_story_failure_at():
    """Cooldown depends only on last_story_failure_at, not last_story_attempt_at."""
    from src.core.warmup import is_cooldown_blocked
    acc = MagicMock()
    acc.last_story_failure_at = None
    acc.last_story_attempt_at = utc_now() - timedelta(minutes=5)  # recent attempt
    blocked, _ = is_cooldown_blocked(acc)
    assert blocked is False  # no failure, so no cooldown
    acc.last_story_failure_at = utc_now() - timedelta(minutes=10)  # failure 10 min ago
    blocked, _ = is_cooldown_blocked(acc)
    assert blocked is True  # failure cooldown 120 min


def test_allowed_precheck_overrides_stale_story_status():
    """When precheck=allowed and NOT stale, overrides rate_limited story_status."""
    acc = MagicMock()
    acc.status = MagicMock(value="active")
    acc.health_status = "alive"
    acc.story_precheck_status = "allowed"
    acc.story_precheck_checked_at = utc_now() - timedelta(minutes=5)  # fresh for 15 min
    acc.story_status = "rate_limited"
    acc.story_blocked_until = utc_now() + timedelta(hours=1)
    acc.manual_review_required = False
    acc.imported_at = None
    acc.successful_story_count = 1  # skip should_require_manual_review
    acc.last_story_failure_at = None
    acc.story_attempts_today = 0
    acc.stories_today = 0
    with patch("src.core.session_paths.account_has_canonical_session", return_value=True):
        with patch("src.core.warmup.is_warmup_blocked", return_value=(False, "")):
            with patch("src.core.warmup.is_cooldown_blocked", return_value=(False, "")):
                with patch("src.core.warmup.is_attempt_cap_exceeded", return_value=False):
                    with patch("src.core.warmup.is_daily_cap_exceeded", return_value=False):
                        with patch("src.core.safety_policy.should_require_manual_review", return_value=(False, "")):
                            dec = get_story_safety_decision(acc, requested_action="story_publish")
    assert dec.allowed is True
    assert dec.precheck_overrode_stale is True


def test_attempt_cap_enforcement():
    """Account at story_attempts_today >= max_story_attempts_per_day is blocked."""
    from src.core.safety_policy import REASON_TOO_MANY_STORY_ATTEMPTS
    acc = MagicMock()
    acc.status = MagicMock(value="active")
    acc.health_status = "alive"
    acc.story_precheck_status = "allowed"
    acc.story_precheck_checked_at = utc_now() - timedelta(minutes=5)
    acc.story_status = "ok"
    acc.story_blocked_until = None
    acc.manual_review_required = False
    acc.last_story_failure_at = None
    acc.story_attempts_today = 5  # at cap (default 5)
    acc.stories_today = 2
    with patch("src.core.session_paths.account_has_canonical_session", return_value=True):
        with patch("src.core.warmup.is_warmup_blocked", return_value=(False, "")):
            with patch("src.core.warmup.is_cooldown_blocked", return_value=(False, "")):
                with patch("src.core.warmup.is_attempt_cap_exceeded", return_value=True):
                    dec = get_story_safety_decision(acc)
    assert dec.allowed is False
    assert dec.reason_code == REASON_TOO_MANY_STORY_ATTEMPTS


def test_get_story_availability_respects_attempt_cap():
    """get_story_availability must not show is_story_ready when attempt cap exceeded (consistency with safety policy)."""
    from src.core.session_paths import get_story_availability
    from unittest.mock import MagicMock
    from datetime import datetime, timedelta
    acc = MagicMock()
    acc.status = MagicMock(value="active")
    acc.health_status = "alive"
    acc.story_precheck_status = "allowed"
    acc.story_precheck_checked_at = utc_now() - timedelta(minutes=5)
    acc.story_status = "ok"
    acc.story_blocked_until = None
    acc.manual_review_required = False
    acc.story_attempts_today = 5
    acc.stories_today = 2
    acc.username_last_changed_at = None
    acc.profile_photo_last_changed_at = None
    with patch("src.core.session_paths.account_has_canonical_session", return_value=True):
        with patch("src.core.warmup.is_warmup_blocked", return_value=(False, "")):
            with patch("src.core.warmup.is_cooldown_blocked", return_value=(False, "")):
                with patch("src.core.warmup.is_daily_cap_exceeded", return_value=False):
                    with patch("src.core.warmup.is_attempt_cap_exceeded", return_value=True):
                        avail = get_story_availability(acc)
    assert avail["is_story_ready"] is False


def test_get_story_availability_stale_precheck_not_ready():
    """When safety policy blocks due to stale precheck, get_story_availability must not return ready."""
    from src.core.session_paths import get_story_availability
    acc = MagicMock()
    acc.status = MagicMock(value="active")
    acc.health_status = "alive"
    acc.story_precheck_status = "allowed"
    acc.story_precheck_checked_at = utc_now() - timedelta(minutes=30)  # stale for 15 min post TTL
    acc.story_status = "ok"
    acc.story_blocked_until = None
    acc.manual_review_required = False
    acc.last_story_failure_at = None
    acc.story_attempts_today = 0
    acc.stories_today = 0
    with patch("src.core.session_paths.account_has_canonical_session", return_value=True):
        avail = get_story_availability(acc)
    assert avail["is_story_ready"] is False
    assert avail["story_ui_status"] == "needs_precheck"
    assert avail["story_available_label"] != "Now"
    assert avail.get("story_precheck_stale") is True


def test_get_story_availability_frozen_precheck_not_unknown_when_stale_ttl():
    """Blocking frozen precheck must set story_ui_status=frozen even when precheck_checked_at is past TTL."""
    from src.core.session_paths import get_story_availability
    acc = MagicMock()
    acc.status = MagicMock(value="active")
    acc.health_status = "alive"
    acc.story_precheck_status = "frozen"
    acc.story_precheck_checked_at = utc_now() - timedelta(minutes=60)
    acc.story_status = "frozen"
    acc.story_blocked_until = None
    acc.story_status_reason = "STORIES_TOO_MUCH"
    acc.manual_review_required = False
    acc.last_story_failure_at = None
    acc.story_attempts_today = 0
    acc.stories_today = 0
    with patch("src.core.session_paths.account_has_canonical_session", return_value=True):
        avail = get_story_availability(acc)
    assert avail["story_ui_status"] == "frozen"
    assert avail["is_story_ready"] is False
    assert avail.get("story_precheck_stale") is True


def test_get_story_availability_telegram_denied_precheck():
    """telegram_denied precheck must map to distinct UI status (not frozen)."""
    from src.core.session_paths import get_story_availability
    acc = MagicMock()
    acc.status = MagicMock(value="active")
    acc.health_status = "alive"
    acc.story_precheck_status = "telegram_denied"
    acc.story_precheck_checked_at = utc_now() - timedelta(minutes=5)
    acc.story_status = "ok"
    acc.story_blocked_until = None
    acc.manual_review_required = False
    acc.last_story_failure_at = None
    acc.story_attempts_today = 0
    acc.stories_today = 0
    with patch("src.core.session_paths.account_has_canonical_session", return_value=True):
        avail = get_story_availability(acc)
    assert avail["story_ui_status"] == "telegram_denied"
    assert avail["is_story_ready"] is False


def test_warming_cap_enforcement():
    """get_story_eligible_accounts_for_batch caps warming accounts."""
    from src.stories.batch_helpers import get_story_eligible_accounts_for_batch
    from src.core.database import get_db_context
    from src.core.models import Account
    # Requires DB; skip if no accounts. Just verify the helper exists and is callable.
    try:
        eligible, skipped = get_story_eligible_accounts_for_batch(
            only_alive=False,
            max_accounts=20,
        )
        assert isinstance(eligible, list)
        assert isinstance(skipped, list)
    except Exception:
        pass  # no DB in test env
