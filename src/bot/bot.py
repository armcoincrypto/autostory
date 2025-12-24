"""
Telegram Bot Dashboard
Alternative control interface via Telegram Bot
"""
import asyncio
from datetime import datetime
from typing import Optional, List
from functools import wraps

from telethon import TelegramClient, events
from telethon.tl.custom import Button
import structlog

import sys
sys.path.insert(0, '/home/user/autostory')
from config.settings import settings
from src.core.models import Account, Story, DiscoveredUser, Campaign, AccountStatus
from src.core.database import get_db_context

logger = structlog.get_logger(__name__)


def admin_only(func):
    """Decorator to restrict commands to admin users"""
    @wraps(func)
    async def wrapper(event, *args, **kwargs):
        sender = await event.get_sender()
        if sender.id not in settings.bot.admin_ids:
            await event.respond("⛔ Unauthorized. You are not an admin.")
            return
        return await func(event, *args, **kwargs)
    return wrapper


class StoryFleetBot:
    """
    Telegram Bot for controlling STORYFLEET

    Features:
    - Account management
    - Story publishing
    - Discovery control
    - Statistics viewing
    """

    def __init__(self):
        self.client: Optional[TelegramClient] = None
        self._running = False

    async def start(self):
        """Start the bot"""
        if not settings.bot.token:
            logger.error("Bot token not configured")
            return False

        self.client = TelegramClient(
            "storyfleet_bot",
            settings.telegram.api_id,
            settings.telegram.api_hash
        )

        await self.client.start(bot_token=settings.bot.token)
        self._register_handlers()
        self._running = True

        logger.info("Bot started", bot_username=await self._get_bot_username())
        return True

    async def stop(self):
        """Stop the bot"""
        if self.client:
            await self.client.disconnect()
            self._running = False
            logger.info("Bot stopped")

    async def run(self):
        """Run the bot until disconnected"""
        if not await self.start():
            return

        logger.info("Bot running...")
        await self.client.run_until_disconnected()

    async def _get_bot_username(self) -> str:
        """Get bot username"""
        me = await self.client.get_me()
        return f"@{me.username}"

    def _register_handlers(self):
        """Register event handlers"""

        @self.client.on(events.NewMessage(pattern="/start"))
        @admin_only
        async def start_handler(event):
            """Handle /start command"""
            await event.respond(
                "🚀 **Welcome to STORYFLEET Control Bot**\n\n"
                "Use the commands below to manage your accounts:\n\n"
                "📊 **Statistics**\n"
                "/stats - View overall statistics\n"
                "/accounts - List all accounts\n\n"
                "📤 **Publishing**\n"
                "/publish - Publish a story\n"
                "/batch - Batch publish stories\n\n"
                "🔍 **Discovery**\n"
                "/scan - Scan a channel\n"
                "/users - View discovered users\n\n"
                "⚙️ **Management**\n"
                "/campaigns - Manage campaigns\n"
                "/help - Show this help",
                buttons=[
                    [Button.text("📊 Stats", resize=True), Button.text("👥 Accounts")],
                    [Button.text("📤 Publish"), Button.text("🔍 Scan")],
                ]
            )

        @self.client.on(events.NewMessage(pattern="/stats"))
        @admin_only
        async def stats_handler(event):
            """Handle /stats command"""
            with get_db_context() as db:
                accounts_total = db.query(Account).count()
                accounts_active = db.query(Account).filter(
                    Account.status == AccountStatus.ACTIVE
                ).count()
                stories_total = db.query(Story).count()
                users_total = db.query(DiscoveredUser).count()
                users_unmentioned = db.query(DiscoveredUser).filter(
                    DiscoveredUser.times_mentioned == 0
                ).count()
                campaigns_active = db.query(Campaign).filter(
                    Campaign.is_active == True
                ).count()

            message = (
                "📊 **STORYFLEET Statistics**\n\n"
                f"👥 **Accounts**: {accounts_active}/{accounts_total} active\n"
                f"📸 **Stories**: {stories_total} published\n"
                f"🔍 **Users**: {users_total} discovered\n"
                f"📤 **Available for mention**: {users_unmentioned}\n"
                f"🎯 **Campaigns**: {campaigns_active} active"
            )

            await event.respond(message)

        @self.client.on(events.NewMessage(pattern="/accounts"))
        @admin_only
        async def accounts_handler(event):
            """Handle /accounts command"""
            with get_db_context() as db:
                accounts = db.query(Account).all()

            if not accounts:
                await event.respond("No accounts added yet.")
                return

            message = "👥 **Accounts**\n\n"
            for acc in accounts[:10]:  # Limit to 10
                status_emoji = {
                    AccountStatus.ACTIVE: "✅",
                    AccountStatus.INACTIVE: "⏸️",
                    AccountStatus.BANNED: "🚫",
                    AccountStatus.FLOOD_WAIT: "⏳",
                    AccountStatus.AUTH_REQUIRED: "🔑",
                }.get(acc.status, "❓")

                message += (
                    f"{status_emoji} **{acc.phone_number}**\n"
                    f"   └ @{acc.username or 'N/A'} | "
                    f"Stories: {acc.stories_today}\n"
                )

            if len(accounts) > 10:
                message += f"\n_...and {len(accounts) - 10} more_"

            await event.respond(message)

        @self.client.on(events.NewMessage(pattern="/scan"))
        @admin_only
        async def scan_handler(event):
            """Handle /scan command"""
            await event.respond(
                "🔍 **Scan Channel for Users**\n\n"
                "Send the channel username to scan:\n"
                "Example: `@channelname`\n\n"
                "Reply with the channel username:",
                buttons=[[Button.text("❌ Cancel")]]
            )

            async with self.client.conversation(event.chat_id) as conv:
                try:
                    response = await conv.get_response(timeout=60)

                    if response.text == "❌ Cancel":
                        await response.respond("Cancelled.")
                        return

                    channel = response.text.strip()

                    await response.respond(f"Scanning {channel}... Please wait.")

                    from src.discovery.scanner import user_discovery
                    result = await user_discovery.discover_from_channels([channel])

                    await response.respond(
                        f"✅ **Scan Complete**\n\n"
                        f"Channel: {channel}\n"
                        f"Scanned: {result.get('total_scanned', 0)} users\n"
                        f"New users: {result.get('total_new_users', 0)}"
                    )

                except asyncio.TimeoutError:
                    await event.respond("Timed out. Please try again.")

        @self.client.on(events.NewMessage(pattern="/publish"))
        @admin_only
        async def publish_handler(event):
            """Handle /publish command"""
            with get_db_context() as db:
                accounts = db.query(Account).filter(
                    Account.status == AccountStatus.ACTIVE
                ).all()

            if not accounts:
                await event.respond("No active accounts available.")
                return

            buttons = [
                [Button.inline(f"📱 {acc.phone_number}", data=f"pub_{acc.id}")]
                for acc in accounts[:5]
            ]
            buttons.append([Button.inline("❌ Cancel", data="cancel")])

            await event.respond(
                "📤 **Publish Story**\n\n"
                "Select an account:",
                buttons=buttons
            )

        @self.client.on(events.CallbackQuery(pattern=r"pub_(\d+)"))
        @admin_only
        async def publish_account_selected(event):
            """Handle account selection for publishing"""
            account_id = int(event.data.decode().split("_")[1])

            await event.edit(
                f"📤 Publishing from Account #{account_id}\n\n"
                "Send the media file (photo/video) for the story:"
            )

            # Store state for next message
            # In a real implementation, you'd use conversation or state management

        @self.client.on(events.NewMessage(pattern="/users"))
        @admin_only
        async def users_handler(event):
            """Handle /users command"""
            with get_db_context() as db:
                users = db.query(DiscoveredUser).filter(
                    DiscoveredUser.times_mentioned == 0
                ).order_by(
                    DiscoveredUser.discovered_at.desc()
                ).limit(10).all()

            if not users:
                await event.respond("No users available for mention.")
                return

            message = "🔍 **Available Users for Mention**\n\n"
            for user in users:
                username = f"@{user.username}" if user.username else f"ID:{user.user_id}"
                message += f"• {username} | {user.first_name or 'N/A'}\n"

            await event.respond(message)

        @self.client.on(events.NewMessage(pattern="/campaigns"))
        @admin_only
        async def campaigns_handler(event):
            """Handle /campaigns command"""
            with get_db_context() as db:
                campaigns = db.query(Campaign).all()

            if not campaigns:
                await event.respond(
                    "No campaigns yet.\n\n"
                    "Create one via the web dashboard at "
                    f"http://localhost:{settings.dashboard.port}"
                )
                return

            message = "🎯 **Campaigns**\n\n"
            for camp in campaigns:
                status = "✅ Active" if camp.is_active else "⏸️ Paused"
                message += (
                    f"**{camp.name}** [{status}]\n"
                    f"   Stories: {camp.total_stories_published} | "
                    f"Mentions: {camp.total_users_mentioned}\n\n"
                )

            await event.respond(message)

        @self.client.on(events.CallbackQuery(pattern="cancel"))
        async def cancel_handler(event):
            """Handle cancel button"""
            await event.edit("Cancelled.")

        @self.client.on(events.NewMessage(pattern="/help"))
        @admin_only
        async def help_handler(event):
            """Handle /help command"""
            await event.respond(
                "📖 **STORYFLEET Commands**\n\n"
                "**General**\n"
                "/start - Welcome message\n"
                "/stats - View statistics\n"
                "/help - This help message\n\n"
                "**Accounts**\n"
                "/accounts - List all accounts\n\n"
                "**Stories**\n"
                "/publish - Publish a story\n"
                "/batch - Batch publish\n\n"
                "**Discovery**\n"
                "/scan - Scan channel for users\n"
                "/users - View available users\n\n"
                "**Campaigns**\n"
                "/campaigns - View campaigns\n\n"
                "🌐 Web Dashboard: http://localhost:5000"
            )

        logger.info("Event handlers registered")


async def run_bot():
    """Entry point to run the bot"""
    bot = StoryFleetBot()
    await bot.run()


if __name__ == "__main__":
    asyncio.run(run_bot())
