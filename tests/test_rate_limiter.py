"""
Tests for Rate Limiter
"""
import pytest
import asyncio
from datetime import datetime, timedelta

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.clients.rate_limiter import RateLimiter, AntiDetection


class TestRateLimiter:
    """Tests for RateLimiter class"""

    @pytest.fixture
    def rate_limiter(self):
        """Create a rate limiter instance"""
        return RateLimiter()

    def test_initial_state(self, rate_limiter):
        """Test initial state for new account"""
        status = rate_limiter.get_status(1)

        assert status["account_id"] == 1
        assert status["action_count"] == 0
        assert status["error_count"] == 0
        assert status["is_blocked"] == False

    @pytest.mark.asyncio
    async def test_wait_records_action(self, rate_limiter):
        """Test that wait records actions"""
        await rate_limiter.wait(1)

        status = rate_limiter.get_status(1)
        assert status["action_count"] == 1
        assert status["last_action"] is not None

    def test_record_success_decreases_errors(self, rate_limiter):
        """Test that success decreases error count"""
        rate_limiter.record_error(1)
        rate_limiter.record_error(1)

        status = rate_limiter.get_status(1)
        assert status["error_count"] == 2

        rate_limiter.record_success(1)

        status = rate_limiter.get_status(1)
        assert status["error_count"] == 1

    def test_flood_wait_blocks_account(self, rate_limiter):
        """Test that flood wait blocks the account"""
        rate_limiter.record_flood_wait(1, 60)

        assert rate_limiter.is_blocked(1) == True

        status = rate_limiter.get_status(1)
        assert status["flood_wait_until"] is not None

    def test_too_many_errors_blocks(self, rate_limiter):
        """Test that too many errors block the account"""
        for _ in range(5):
            rate_limiter.record_error(1)

        assert rate_limiter.is_blocked(1) == True

    def test_reset_clears_state(self, rate_limiter):
        """Test that reset clears all state"""
        rate_limiter.record_error(1)
        rate_limiter.record_flood_wait(1, 60)

        rate_limiter.reset(1)

        status = rate_limiter.get_status(1)
        assert status["error_count"] == 0
        assert status["is_blocked"] == False

    def test_rate_limiter_rebinds_lock_across_sequential_run_calls(self, rate_limiter):
        """Flask run_async creates a new loop per request; shared limiter must not raise on asyncio.Lock."""
        async def tick(aid: int):
            await rate_limiter.wait(aid)

        asyncio.run(tick(42))
        asyncio.run(tick(42))
        assert rate_limiter.get_status(42)["action_count"] == 2

    @pytest.mark.asyncio
    async def test_wait_for_healthcheck_skips_long_inter_action_delay(self, rate_limiter):
        """wait() is fast when enough time already elapsed since last_action.

        Public API has no for_healthcheck kwarg; short health-like steps rely on
        the normal delay math (actual_delay = max(0, base_delay - time_since_last)).
        """
        import time

        rate_limiter.min_delay = 30.0
        rate_limiter.max_delay = 60.0
        rate_limiter.randomize = False
        state = rate_limiter._get_state(77)
        state.last_action = datetime.utcnow() - timedelta(seconds=120)
        state.action_count = 0

        t0 = time.monotonic()
        await rate_limiter.wait(77)
        assert time.monotonic() - t0 < 2.0
        assert rate_limiter.get_status(77)["action_count"] == 1

        # Age last_action again so the second wait also skips the long base delay.
        state.last_action = datetime.utcnow() - timedelta(seconds=120)
        t1 = time.monotonic()
        await rate_limiter.wait(77)
        assert time.monotonic() - t1 < 2.0
        assert rate_limiter.get_status(77)["action_count"] == 2


class TestAntiDetection:
    """Tests for AntiDetection class"""

    @pytest.mark.asyncio
    async def test_random_pause(self):
        """Test random pause function"""
        start = datetime.utcnow()
        await AntiDetection.random_pause(0.1, 0.2)
        elapsed = (datetime.utcnow() - start).total_seconds()

        assert elapsed >= 0.1
        assert elapsed < 0.3  # Allow some overhead

    def test_randomize_message(self):
        """Test message randomization"""
        original = "Test message"
        randomized = AntiDetection.randomize_message(original)

        # Should contain the original text
        assert original.rstrip() in randomized.rstrip()
