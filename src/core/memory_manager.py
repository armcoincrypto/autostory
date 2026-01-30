"""
SECURITY HOTFIX: Memory leak prevention for pending operations
Fixes MEDIUM vulnerability: Memory leak in global dictionaries
"""
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Any
import structlog

logger = structlog.get_logger(__name__)


class MemoryManager:
    """
    Clean up orphaned pending operations to prevent memory leaks

    The bot uses global dictionaries to track pending operations:
    - pending_logins
    - pending_scans
    - pending_publishes
    - pending_tdata_imports

    These can grow indefinitely if users abandon operations.
    This manager periodically cleans up expired entries.
    """

    def __init__(self, cleanup_interval: int = 300, max_age_minutes: int = 30):
        """
        Initialize memory manager

        Args:
            cleanup_interval: Seconds between cleanup runs (default 5 min)
            max_age_minutes: Max age of pending operations before cleanup
        """
        self.cleanup_interval = cleanup_interval
        self.max_age = timedelta(minutes=max_age_minutes)
        self.is_running = False
        self._cleanup_task = None
        self._stats = {
            'total_cleaned': 0,
            'last_cleanup': None,
            'cleanup_runs': 0,
        }

    async def start_cleanup(self, pending_dicts: Dict[str, Dict[int, Any]]):
        """
        Start background cleanup task

        Args:
            pending_dicts: Dictionary of pending operation dicts to monitor
                          Format: {'name': pending_dict, ...}
        """
        if self.is_running:
            logger.warning("Memory manager already running")
            return

        self.is_running = True
        self._cleanup_task = asyncio.create_task(
            self._cleanup_loop(pending_dicts)
        )
        logger.info("Memory manager started",
                   interval=self.cleanup_interval,
                   max_age_minutes=self.max_age.total_seconds() / 60)

    async def _cleanup_loop(self, pending_dicts: Dict[str, Dict[int, Any]]):
        """Background cleanup loop"""
        while self.is_running:
            try:
                await asyncio.sleep(self.cleanup_interval)
                await self._cleanup_expired(pending_dicts)
                self._stats['cleanup_runs'] += 1
                self._stats['last_cleanup'] = datetime.utcnow().isoformat()

            except asyncio.CancelledError:
                logger.info("Memory manager cleanup cancelled")
                break
            except Exception as e:
                logger.error("Memory manager error", error=str(e))
                await asyncio.sleep(60)  # Wait before retry

    async def _cleanup_expired(self, pending_dicts: Dict[str, Dict[int, Any]]):
        """
        Clean up expired pending operations

        For each pending operation:
        1. Check if it has a 'created_at' timestamp
        2. If older than max_age, clean up resources and remove
        3. Disconnect any Telegram clients
        """
        now = datetime.utcnow()
        total_expired = 0

        for dict_name, pending_dict in pending_dicts.items():
            expired_users = []

            # Find expired entries
            for user_id, state in list(pending_dict.items()):
                try:
                    # Check creation time
                    created_at = state.get('created_at')
                    if created_at:
                        if isinstance(created_at, str):
                            created_at = datetime.fromisoformat(created_at)

                        if (now - created_at) > self.max_age:
                            expired_users.append(user_id)

                            # Clean up Telegram client if exists
                            if 'client' in state:
                                try:
                                    client = state['client']
                                    if hasattr(client, 'disconnect'):
                                        await asyncio.wait_for(
                                            client.disconnect(),
                                            timeout=5.0
                                        )
                                        logger.debug("Disconnected orphaned client",
                                                   user_id=user_id)
                                except asyncio.TimeoutError:
                                    logger.warning("Client disconnect timeout",
                                                 user_id=user_id)
                                except Exception as e:
                                    logger.debug("Client disconnect error",
                                               user_id=user_id, error=str(e))

                    # Also clean up entries without created_at that have been around
                    # (legacy entries before we added timestamps)
                    elif not created_at:
                        # Add timestamp for tracking
                        state['created_at'] = now

                except Exception as e:
                    logger.error("Error checking pending entry",
                               dict_name=dict_name, user_id=user_id, error=str(e))

            # Remove expired entries
            for user_id in expired_users:
                try:
                    del pending_dict[user_id]
                    total_expired += 1
                except KeyError:
                    pass

            if expired_users:
                logger.info("Cleaned expired pending operations",
                          dict_name=dict_name,
                          count=len(expired_users))

        if total_expired > 0:
            self._stats['total_cleaned'] += total_expired
            logger.info("Memory cleanup complete",
                       total_cleaned=total_expired,
                       lifetime_cleaned=self._stats['total_cleaned'])

    async def stop(self):
        """Stop cleanup task gracefully"""
        self.is_running = False
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        logger.info("Memory manager stopped", stats=self._stats)

    def get_stats(self) -> Dict[str, Any]:
        """Get cleanup statistics"""
        return self._stats.copy()


# Global instance for easy access
memory_manager = MemoryManager()
