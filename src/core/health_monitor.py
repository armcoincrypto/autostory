"""
Health Monitor - Monitor system and account health
Sends alerts when issues are detected
"""
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any, Callable
from enum import Enum

from telethon import TelegramClient
from telethon.errors import (
    AuthKeyUnregisteredError,
    UserDeactivatedBanError,
    SessionRevokedError,
)
import structlog

import sys
sys.path.insert(0, '/home/user/autostory')
from config.settings import settings
from src.core.models import Account, AccountStatus
from src.core.database import get_db_context
from src.core.session_manager import session_manager

logger = structlog.get_logger(__name__)


class AlertLevel(Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class HealthMonitor:
    """
    Monitors account and system health

    Features:
    - Periodic account health checks
    - Detect banned/limited accounts
    - Send alerts via Telegram
    - Auto-disable problematic accounts
    """

    def __init__(self):
        self._running = False
        self._check_interval = 600  # 10 minutes
        self._alert_bot: Optional[TelegramClient] = None
        self._alert_chat_id: Optional[int] = None

    async def start(self, alert_bot: TelegramClient = None, alert_chat_id: int = None):
        """Start the health monitor"""
        self._alert_bot = alert_bot
        self._alert_chat_id = alert_chat_id or (
            settings.bot.admin_ids[0] if settings.bot.admin_ids else None
        )

        self._running = True
        logger.info("Health monitor started")

        # Start background monitoring
        asyncio.create_task(self._monitoring_loop())

    async def stop(self):
        """Stop the health monitor"""
        self._running = False
        logger.info("Health monitor stopped")

    async def _monitoring_loop(self):
        """Background monitoring loop"""
        while self._running:
            try:
                await self.check_all_accounts()
            except Exception as e:
                logger.error("Health check error", error=str(e))

            await asyncio.sleep(self._check_interval)

    async def check_all_accounts(self) -> Dict[str, Any]:
        """Check health of all accounts"""
        results = {
            "timestamp": datetime.utcnow().isoformat(),
            "total": 0,
            "healthy": 0,
            "issues": [],
        }

        with get_db_context() as db:
            accounts = db.query(Account).filter(
                Account.session_string.isnot(None)
            ).all()

            results["total"] = len(accounts)

            for account in accounts:
                status = await self._check_account_health(account)

                if status["healthy"]:
                    results["healthy"] += 1
                else:
                    results["issues"].append({
                        "account_id": account.id,
                        "phone": account.phone_number,
                        "issue": status["issue"],
                        "action": status["action"],
                    })

                    # Send alert for critical issues
                    if status["level"] == AlertLevel.CRITICAL:
                        await self._send_alert(
                            f"🚨 **CRITICAL: Account Issue**\n\n"
                            f"Phone: {account.phone_number}\n"
                            f"Issue: {status['issue']}\n"
                            f"Action: {status['action']}"
                        )

        logger.info(
            "Health check complete",
            total=results["total"],
            healthy=results["healthy"],
            issues=len(results["issues"])
        )

        return results

    async def _check_account_health(self, account: Account) -> Dict[str, Any]:
        """Check health of a single account"""
        result = {
            "healthy": True,
            "issue": None,
            "action": None,
            "level": AlertLevel.INFO,
        }

        try:
            # Get client
            client = await session_manager.get_client(account.id)

            if not client:
                result["healthy"] = False
                result["issue"] = "Session invalid or expired"
                result["action"] = "Re-login required"
                result["level"] = AlertLevel.WARNING
                await self._mark_account_status(account.id, AccountStatus.AUTH_REQUIRED)
                return result

            # Check if authorized
            if not await client.is_user_authorized():
                result["healthy"] = False
                result["issue"] = "Session no longer authorized"
                result["action"] = "Re-login required"
                result["level"] = AlertLevel.WARNING
                await self._mark_account_status(account.id, AccountStatus.AUTH_REQUIRED)
                return result

            # Try to get self (verifies account is working)
            try:
                me = await client.get_me()
                if not me:
                    raise Exception("Failed to get account info")
            except AuthKeyUnregisteredError:
                result["healthy"] = False
                result["issue"] = "Auth key invalid"
                result["action"] = "Re-login required"
                result["level"] = AlertLevel.WARNING
                await self._mark_account_status(account.id, AccountStatus.AUTH_REQUIRED)
                return result
            except UserDeactivatedBanError:
                result["healthy"] = False
                result["issue"] = "Account BANNED"
                result["action"] = "Account disabled, remove from system"
                result["level"] = AlertLevel.CRITICAL
                await self._mark_account_status(account.id, AccountStatus.BANNED)
                return result
            except SessionRevokedError:
                result["healthy"] = False
                result["issue"] = "Session revoked"
                result["action"] = "Re-login required"
                result["level"] = AlertLevel.WARNING
                await self._mark_account_status(account.id, AccountStatus.AUTH_REQUIRED)
                return result

            # Check flood wait status
            if account.status == AccountStatus.FLOOD_WAIT:
                if account.flood_wait_until and account.flood_wait_until > datetime.utcnow():
                    result["healthy"] = False
                    result["issue"] = f"Rate limited until {account.flood_wait_until}"
                    result["action"] = "Waiting for cooldown"
                    result["level"] = AlertLevel.WARNING
                else:
                    # Flood wait expired, restore to active
                    await self._mark_account_status(account.id, AccountStatus.ACTIVE)

            # Check daily story limits
            if account.stories_today >= settings.telegram.max_stories_per_hour * 24:
                result["healthy"] = False
                result["issue"] = "Daily story limit reached"
                result["action"] = "Wait until tomorrow"
                result["level"] = AlertLevel.INFO

            # Account is healthy, ensure marked as active
            if result["healthy"] and account.status != AccountStatus.ACTIVE:
                await self._mark_account_status(account.id, AccountStatus.ACTIVE)

        except Exception as e:
            result["healthy"] = False
            result["issue"] = f"Check failed: {str(e)}"
            result["action"] = "Investigate error"
            result["level"] = AlertLevel.WARNING
            logger.error("Account health check failed", account_id=account.id, error=str(e))

        return result

    async def _mark_account_status(self, account_id: int, status: AccountStatus):
        """Update account status in database"""
        try:
            with get_db_context() as db:
                account = db.query(Account).filter(Account.id == account_id).first()
                if account and account.status != status:
                    old_status = account.status
                    account.status = status
                    db.commit()
                    logger.info(
                        "Account status changed",
                        account_id=account_id,
                        old_status=old_status,
                        new_status=status
                    )
        except Exception as e:
            logger.error("Failed to update account status", error=str(e))

    async def _send_alert(self, message: str):
        """Send alert to admin via Telegram"""
        if not self._alert_bot or not self._alert_chat_id:
            logger.warning("Alert bot not configured, logging only")
            logger.warning(f"ALERT: {message}")
            return

        try:
            await self._alert_bot.send_message(
                self._alert_chat_id,
                message,
                parse_mode='markdown'
            )
        except Exception as e:
            logger.error("Failed to send alert", error=str(e))

    async def get_system_status(self) -> Dict[str, Any]:
        """Get overall system status"""
        with get_db_context() as db:
            total_accounts = db.query(Account).count()
            active_accounts = db.query(Account).filter(
                Account.status == AccountStatus.ACTIVE
            ).count()
            banned_accounts = db.query(Account).filter(
                Account.status == AccountStatus.BANNED
            ).count()
            flood_wait_accounts = db.query(Account).filter(
                Account.status == AccountStatus.FLOOD_WAIT
            ).count()
            auth_required = db.query(Account).filter(
                Account.status == AccountStatus.AUTH_REQUIRED
            ).count()

            from src.core.models import DiscoveredUser, Story
            total_users = db.query(DiscoveredUser).count()
            total_stories = db.query(Story).count()

        return {
            "timestamp": datetime.utcnow().isoformat(),
            "accounts": {
                "total": total_accounts,
                "active": active_accounts,
                "banned": banned_accounts,
                "flood_wait": flood_wait_accounts,
                "auth_required": auth_required,
            },
            "users_discovered": total_users,
            "stories_published": total_stories,
            "health": "good" if banned_accounts == 0 and auth_required == 0 else "issues",
        }


# Global instance
health_monitor = HealthMonitor()
