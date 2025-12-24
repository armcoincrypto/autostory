"""
Rate Limiter - Anti-detection and flood prevention
"""
import asyncio
import random
from datetime import datetime, timedelta
from typing import Dict, Optional
from dataclasses import dataclass, field
import structlog

import sys
sys.path.insert(0, '/home/user/autostory')
from config.settings import settings

logger = structlog.get_logger(__name__)


@dataclass
class AccountRateState:
    """Rate limiting state for an account"""
    last_action: Optional[datetime] = None
    action_count: int = 0
    error_count: int = 0
    flood_wait_until: Optional[datetime] = None
    daily_reset: Optional[datetime] = None

    def reset_daily(self) -> None:
        """Reset daily counters"""
        self.action_count = 0
        self.error_count = 0
        self.daily_reset = datetime.utcnow()


class RateLimiter:
    """
    Intelligent rate limiter with anti-detection features

    Features:
    - Per-account rate tracking
    - Randomized delays to avoid patterns
    - Flood wait handling
    - Daily action limits
    - Error backoff
    """

    def __init__(self):
        self._states: Dict[int, AccountRateState] = {}
        self._global_lock = asyncio.Lock()
        self._account_locks: Dict[int, asyncio.Lock] = {}

        # Configuration
        self.min_delay = settings.telegram.min_delay_between_actions
        self.max_delay = settings.telegram.max_delay_between_actions
        self.max_stories_per_hour = settings.telegram.max_stories_per_hour
        self.randomize = settings.telegram.randomize_delays

    def _get_state(self, account_id: int) -> AccountRateState:
        """Get or create rate state for account"""
        if account_id not in self._states:
            self._states[account_id] = AccountRateState()
        return self._states[account_id]

    def _get_lock(self, account_id: int) -> asyncio.Lock:
        """Get or create lock for account"""
        if account_id not in self._account_locks:
            self._account_locks[account_id] = asyncio.Lock()
        return self._account_locks[account_id]

    async def wait(self, account_id: int) -> float:
        """
        Wait before performing an action
        Returns the actual wait time in seconds
        """
        lock = self._get_lock(account_id)
        async with lock:
            state = self._get_state(account_id)

            # Check if in flood wait
            if state.flood_wait_until and datetime.utcnow() < state.flood_wait_until:
                wait_seconds = (state.flood_wait_until - datetime.utcnow()).total_seconds()
                logger.info(
                    "Account in flood wait",
                    account_id=account_id,
                    wait_seconds=wait_seconds
                )
                await asyncio.sleep(wait_seconds)
                state.flood_wait_until = None

            # Reset daily counters if needed
            if not state.daily_reset or (datetime.utcnow() - state.daily_reset).days >= 1:
                state.reset_daily()

            # Calculate delay
            if state.last_action:
                time_since_last = (datetime.utcnow() - state.last_action).total_seconds()
            else:
                time_since_last = self.max_delay  # First action, no wait

            # Base delay
            base_delay = self._calculate_delay(state)

            # Subtract time already waited
            actual_delay = max(0, base_delay - time_since_last)

            if actual_delay > 0:
                if self.randomize:
                    # Add randomization (-20% to +30%)
                    variance = random.uniform(-0.2, 0.3)
                    actual_delay *= (1 + variance)

                logger.debug(
                    "Rate limiting",
                    account_id=account_id,
                    delay=actual_delay
                )
                await asyncio.sleep(actual_delay)

            state.last_action = datetime.utcnow()
            state.action_count += 1

            return actual_delay

    def _calculate_delay(self, state: AccountRateState) -> float:
        """Calculate appropriate delay based on state"""
        base_delay = random.uniform(self.min_delay, self.max_delay)

        # Increase delay after errors
        if state.error_count > 0:
            error_multiplier = min(3, 1 + (state.error_count * 0.5))
            base_delay *= error_multiplier

        # Slow down if many actions today
        if state.action_count > 50:
            action_multiplier = 1 + (state.action_count / 100)
            base_delay *= min(2, action_multiplier)

        return base_delay

    def record_success(self, account_id: int) -> None:
        """Record successful action"""
        state = self._get_state(account_id)
        state.error_count = max(0, state.error_count - 1)  # Reduce error count on success

    def record_error(self, account_id: int) -> None:
        """Record failed action"""
        state = self._get_state(account_id)
        state.error_count += 1
        logger.warning(
            "Error recorded",
            account_id=account_id,
            error_count=state.error_count
        )

    def record_flood_wait(self, account_id: int, seconds: int) -> None:
        """Record flood wait from Telegram"""
        state = self._get_state(account_id)
        state.flood_wait_until = datetime.utcnow() + timedelta(seconds=seconds)
        state.error_count += 2  # Flood wait is more serious
        logger.warning(
            "Flood wait recorded",
            account_id=account_id,
            until=state.flood_wait_until
        )

    def is_blocked(self, account_id: int) -> bool:
        """Check if account is currently rate limited"""
        state = self._get_state(account_id)

        # Check flood wait
        if state.flood_wait_until and datetime.utcnow() < state.flood_wait_until:
            return True

        # Check error count (too many errors = temporary block)
        if state.error_count >= 5:
            return True

        return False

    def get_status(self, account_id: int) -> Dict:
        """Get rate limiting status for account"""
        state = self._get_state(account_id)
        return {
            "account_id": account_id,
            "action_count": state.action_count,
            "error_count": state.error_count,
            "is_blocked": self.is_blocked(account_id),
            "flood_wait_until": state.flood_wait_until.isoformat() if state.flood_wait_until else None,
            "last_action": state.last_action.isoformat() if state.last_action else None,
        }

    def reset(self, account_id: int) -> None:
        """Reset rate limiting state for account"""
        if account_id in self._states:
            del self._states[account_id]
        logger.info("Rate limit reset", account_id=account_id)


class AntiDetection:
    """
    Additional anti-detection measures
    """

    @staticmethod
    async def simulate_typing(
        client,
        chat,
        duration_range: tuple = (1.0, 3.0)
    ) -> None:
        """Simulate typing indicator"""
        if settings.telegram.simulate_typing:
            duration = random.uniform(*duration_range)
            async with client.action(chat, 'typing'):
                await asyncio.sleep(duration)

    @staticmethod
    async def random_pause(min_seconds: float = 0.5, max_seconds: float = 2.0) -> None:
        """Add random pause between operations"""
        await asyncio.sleep(random.uniform(min_seconds, max_seconds))

    @staticmethod
    def randomize_message(text: str) -> str:
        """
        Add minor variations to text to avoid duplicate detection
        - Random invisible characters
        - Random punctuation variations
        """
        variations = [
            lambda t: t + "\u200b",  # Zero-width space
            lambda t: t.rstrip() + " ",  # Trailing space
            lambda t: t,  # No change
        ]
        return random.choice(variations)(text)
