"""
Telegram Bot Dashboard
Alternative control interface via Telegram Bot with User Login Flow
"""
import asyncio
from datetime import datetime
from typing import Optional, Dict, Any
from functools import wraps

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.custom import Button
from telethon.errors import (
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    SessionPasswordNeededError,
    PasswordHashInvalidError,
    FloodWaitError,
    PhoneNumberInvalidError,
    PhoneNumberBannedError,
)
import structlog

import sys
sys.path.insert(0, '/home/user/autostory')
from config.settings import settings
from src.core.models import Account, Story, DiscoveredUser, Campaign, AccountStatus
from src.core.database import get_db_context

logger = structlog.get_logger(__name__)


# Store for pending login sessions (user_id -> login state)
pending_logins: Dict[int, Dict[str, Any]] = {}


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
    - User Account Login/Authorization via Bot
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
            print("❌ Bot token not configured. Check your .env file.")
            return False

        self.client = TelegramClient(
            "storyfleet_bot",
            settings.telegram.api_id,
            settings.telegram.api_hash
        )

        await self.client.start(bot_token=settings.bot.token)
        self._register_handlers()
        self._running = True

        bot_username = await self._get_bot_username()
        logger.info("Bot started", bot_username=bot_username)
        print(f"✅ Bot started: {bot_username}")
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
        print("🚀 Bot is running. Press Ctrl+C to stop.")
        await self.client.run_until_disconnected()

    async def _get_bot_username(self) -> str:
        """Get bot username"""
        me = await self.client.get_me()
        return f"@{me.username}"

    async def _handle_phone_step(self, event, user_id: int):
        """Handle phone number input"""
        phone = event.text.strip()

        if not phone.startswith("+"):
            phone = "+" + phone

        phone = phone.replace(" ", "").replace("-", "")

        await event.respond(f"📞 Connecting to Telegram with {phone}...")

        try:
            user_client = TelegramClient(
                StringSession(),
                settings.telegram.api_id,
                settings.telegram.api_hash
            )

            await user_client.connect()
            result = await user_client.send_code_request(phone)

            pending_logins[user_id] = {
                "step": "code",
                "client": user_client,
                "phone": phone,
                "phone_code_hash": result.phone_code_hash,
            }

            await event.respond(
                f"✅ **Verification code sent!**\n\n"
                f"A code has been sent to {phone} via Telegram.\n\n"
                "Please enter the code you received:\n\n"
                "💡 _Tip: Enter the code with spaces or as-is_\n"
                "Example: `1 2 3 4 5` or `12345`\n\n"
                "Send /cancel to abort.",
                buttons=[[Button.text("❌ Cancel")]]
            )

        except PhoneNumberInvalidError:
            await event.respond(
                "❌ **Invalid phone number**\n\n"
                "Please enter a valid phone number with country code.\n"
                "Example: `+1234567890`"
            )
            pending_logins[user_id]["step"] = "phone"

        except PhoneNumberBannedError:
            await event.respond(
                "🚫 **Phone number is banned**\n\n"
                "This phone number has been banned from Telegram.\n"
                "Please try a different number."
            )
            del pending_logins[user_id]

        except FloodWaitError as e:
            await event.respond(
                f"⏳ **Too many attempts**\n\n"
                f"Please wait {e.seconds} seconds before trying again."
            )
            del pending_logins[user_id]

        except Exception as e:
            logger.error("Phone step error", error=str(e))
            await event.respond(
                f"❌ **Error**: {str(e)}\n\n"
                "Please try again with /login"
            )
            del pending_logins[user_id]

    async def _handle_code_step(self, event, user_id: int):
        """Handle verification code input"""
        code = event.text.strip().replace(" ", "").replace("-", "")

        login_state = pending_logins[user_id]
        user_client = login_state["client"]
        phone = login_state["phone"]
        phone_code_hash = login_state["phone_code_hash"]

        await event.respond("🔐 Verifying code...")

        try:
            await user_client.sign_in(
                phone=phone,
                code=code,
                phone_code_hash=phone_code_hash
            )

            await self._save_account(event, user_id, user_client, phone)

        except PhoneCodeInvalidError:
            await event.respond(
                "❌ **Invalid code**\n\n"
                "The code you entered is incorrect.\n"
                "Please check and try again.\n\n"
                "Enter the correct code or /cancel to abort."
            )

        except PhoneCodeExpiredError:
            await event.respond(
                "⏰ **Code expired**\n\n"
                "The verification code has expired.\n"
                "Please start again with /login"
            )
            try:
                await user_client.disconnect()
            except:
                pass
            del pending_logins[user_id]

        except SessionPasswordNeededError:
            pending_logins[user_id]["step"] = "2fa"
            await event.respond(
                "🔒 **Two-Factor Authentication**\n\n"
                "This account has 2FA enabled.\n"
                "Please enter your 2FA password:\n\n"
                "Send /cancel to abort.",
                buttons=[[Button.text("❌ Cancel")]]
            )

        except Exception as e:
            logger.error("Code step error", error=str(e))
            await event.respond(
                f"❌ **Error**: {str(e)}\n\n"
                "Please try again with /login"
            )
            try:
                await user_client.disconnect()
            except:
                pass
            del pending_logins[user_id]

    async def _handle_2fa_step(self, event, user_id: int):
        """Handle 2FA password input"""
        password = event.text.strip()

        login_state = pending_logins[user_id]
        user_client = login_state["client"]
        phone = login_state["phone"]

        await event.respond("🔐 Verifying password...")

        try:
            await user_client.sign_in(password=password)
            await self._save_account(event, user_id, user_client, phone)

        except PasswordHashInvalidError:
            await event.respond(
                "❌ **Invalid password**\n\n"
                "The 2FA password is incorrect.\n"
                "Please try again.\n\n"
                "Send /cancel to abort."
            )

        except Exception as e:
            logger.error("2FA step error", error=str(e))
            await event.respond(
                f"❌ **Error**: {str(e)}\n\n"
                "Please try again with /login"
            )
            try:
                await user_client.disconnect()
            except:
                pass
            del pending_logins[user_id]

    async def _save_account(self, event, user_id: int, user_client: TelegramClient, phone: str):
        """Save the authenticated account to database"""
        try:
            me = await user_client.get_me()
            session_string = user_client.session.save()

            with get_db_context() as db:
                existing = db.query(Account).filter(
                    Account.phone_number == phone
                ).first()

                if existing:
                    existing.session_string = session_string
                    existing.user_id = me.id
                    existing.username = me.username
                    existing.first_name = me.first_name
                    existing.last_name = me.last_name
                    existing.status = AccountStatus.ACTIVE
                    existing.last_used = datetime.utcnow()
                    db.commit()
                    account_id = existing.id
                    is_new = False
                else:
                    new_account = Account(
                        phone_number=phone,
                        session_string=session_string,
                        user_id=me.id,
                        username=me.username,
                        first_name=me.first_name,
                        last_name=me.last_name,
                        status=AccountStatus.ACTIVE,
                    )
                    db.add(new_account)
                    db.commit()
                    account_id = new_account.id
                    is_new = True

            await user_client.disconnect()
            del pending_logins[user_id]

            username_str = f"@{me.username}" if me.username else "No username"
            name_str = f"{me.first_name or ''} {me.last_name or ''}".strip() or "N/A"
            action = "added" if is_new else "updated"

            await event.respond(
                f"✅ **Account Successfully {action.title()}!**\n\n"
                f"📱 **Phone**: {phone}\n"
                f"👤 **Name**: {name_str}\n"
                f"🔗 **Username**: {username_str}\n"
                f"🆔 **User ID**: {me.id}\n"
                f"📊 **Account ID**: #{account_id}\n\n"
                f"The account is now ready to use for story publishing!",
                buttons=[
                    [Button.text("📱 Add Another", resize=True), Button.text("👥 View Accounts")],
                    [Button.text("📊 Stats"), Button.text("📤 Publish Story")],
                ]
            )

            logger.info(
                "Account saved",
                phone=phone,
                username=me.username,
                account_id=account_id,
                is_new=is_new
            )

        except Exception as e:
            logger.error("Save account error", error=str(e))
            await event.respond(
                f"❌ **Error saving account**: {str(e)}\n\n"
                "The login was successful but failed to save.\n"
                "Please try again with /login"
            )
            try:
                await user_client.disconnect()
            except:
                pass
            if user_id in pending_logins:
                del pending_logins[user_id]

    def _register_handlers(self):
        """Register all event handlers"""

        @self.client.on(events.NewMessage(pattern="/start"))
        async def start_handler(event):
            """Handle /start command"""
            sender = await event.get_sender()
            is_admin = sender.id in settings.bot.admin_ids

            welcome = (
                "🚀 **Welcome to STORYFLEET**\n\n"
                "Multi-Account Telegram Story Orchestration Platform\n\n"
            )

            if is_admin:
                welcome += (
                    "**Admin Commands:**\n"
                    "📱 /login - Add a new Telegram account\n"
                    "👥 /accounts - View all accounts\n"
                    "📊 /stats - View statistics\n"
                    "📤 /publish - Publish a story\n"
                    "🔍 /scan - Scan channel for users\n"
                    "🎯 /campaigns - View campaigns\n"
                    "❓ /help - Full command list"
                )
            else:
                welcome += "⛔ You are not authorized to use this bot."

            await event.respond(
                welcome,
                buttons=[
                    [Button.text("📱 Login Account", resize=True), Button.text("👥 Accounts")],
                    [Button.text("📊 Stats"), Button.text("❓ Help")],
                ] if is_admin else None
            )

        @self.client.on(events.NewMessage(pattern="/login"))
        @admin_only
        async def login_handler(event):
            """Start the login flow"""
            sender = await event.get_sender()
            user_id = sender.id

            if user_id in pending_logins:
                old_client = pending_logins[user_id].get("client")
                if old_client:
                    try:
                        await old_client.disconnect()
                    except:
                        pass
                del pending_logins[user_id]

            pending_logins[user_id] = {
                "step": "phone",
                "client": None,
                "phone": None,
                "phone_code_hash": None,
            }

            await event.respond(
                "📱 **Add New Telegram Account**\n\n"
                "Please enter the phone number with country code:\n\n"
                "Example: `+1234567890`\n\n"
                "Send /cancel to abort.",
                buttons=[[Button.text("❌ Cancel")]]
            )

        @self.client.on(events.NewMessage(pattern="/cancel"))
        @admin_only
        async def cancel_login_handler(event):
            """Cancel ongoing login"""
            sender = await event.get_sender()
            user_id = sender.id

            if user_id in pending_logins:
                old_client = pending_logins[user_id].get("client")
                if old_client:
                    try:
                        await old_client.disconnect()
                    except:
                        pass
                del pending_logins[user_id]
                await event.respond("❌ Login cancelled.")
            else:
                await event.respond("No active login process.")

        @self.client.on(events.NewMessage())
        async def message_handler(event):
            """Handle messages for login flow"""
            if event.text and event.text.startswith("/"):
                return

            sender = await event.get_sender()
            if not sender:
                return

            user_id = sender.id

            if user_id not in settings.bot.admin_ids:
                return

            if user_id not in pending_logins:
                return

            login_state = pending_logins[user_id]
            step = login_state.get("step")

            if event.text == "❌ Cancel":
                if user_id in pending_logins:
                    old_client = pending_logins[user_id].get("client")
                    if old_client:
                        try:
                            await old_client.disconnect()
                        except:
                            pass
                    del pending_logins[user_id]
                await event.respond("❌ Login cancelled.")
                return

            if step == "phone":
                await self._handle_phone_step(event, user_id)
            elif step == "code":
                await self._handle_code_step(event, user_id)
            elif step == "2fa":
                await self._handle_2fa_step(event, user_id)

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
                await event.respond(
                    "No accounts added yet.\n\n"
                    "Use /login to add your first Telegram account."
                )
                return

            message = "👥 **Registered Accounts**\n\n"
            for acc in accounts[:10]:
                status_emoji = {
                    AccountStatus.ACTIVE: "✅",
                    AccountStatus.INACTIVE: "⏸️",
                    AccountStatus.BANNED: "🚫",
                    AccountStatus.FLOOD_WAIT: "⏳",
                    AccountStatus.AUTH_REQUIRED: "🔑",
                }.get(acc.status, "❓")

                username_str = f"@{acc.username}" if acc.username else "No username"

                message += (
                    f"{status_emoji} **{acc.phone_number}**\n"
                    f"   └ {username_str} | "
                    f"Stories: {acc.stories_today}\n"
                )

            if len(accounts) > 10:
                message += f"\n_...and {len(accounts) - 10} more_"

            message += "\n\nUse /login to add more accounts."
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

        @self.client.on(events.NewMessage(pattern="/publish"))
        @admin_only
        async def publish_handler(event):
            """Handle /publish command"""
            with get_db_context() as db:
                accounts = db.query(Account).filter(
                    Account.status == AccountStatus.ACTIVE
                ).all()

            if not accounts:
                await event.respond(
                    "No active accounts available.\n\n"
                    "Use /login to add an account first."
                )
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
                    "Use the web dashboard to create campaigns."
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
                "**Account Management**\n"
                "📱 /login - Add new Telegram account\n"
                "👥 /accounts - List all accounts\n"
                "/cancel - Cancel current operation\n\n"
                "**Statistics**\n"
                "📊 /stats - View overall statistics\n\n"
                "**Stories**\n"
                "📤 /publish - Publish a story\n\n"
                "**Discovery**\n"
                "🔍 /scan - Scan channel for users\n"
                "/users - View available users\n\n"
                "**Campaigns**\n"
                "🎯 /campaigns - View campaigns\n\n"
                "**Other**\n"
                "/start - Welcome message\n"
                "/help - This help message"
            )

        # Button handlers
        @self.client.on(events.NewMessage(pattern="📱 Login Account"))
        @admin_only
        async def login_button_handler(event):
            """Handle Login Account button"""
            sender = await event.get_sender()
            pending_logins[sender.id] = {
                "step": "phone",
                "client": None,
                "phone": None,
                "phone_code_hash": None,
            }
            await event.respond(
                "📱 **Add New Telegram Account**\n\n"
                "Please enter the phone number with country code:\n\n"
                "Example: `+1234567890`\n\n"
                "Send /cancel to abort.",
                buttons=[[Button.text("❌ Cancel")]]
            )

        @self.client.on(events.NewMessage(pattern="📱 Add Another"))
        @admin_only
        async def add_another_handler(event):
            """Handle Add Another button"""
            sender = await event.get_sender()
            pending_logins[sender.id] = {
                "step": "phone",
                "client": None,
                "phone": None,
                "phone_code_hash": None,
            }
            await event.respond(
                "📱 **Add New Telegram Account**\n\n"
                "Please enter the phone number with country code:\n\n"
                "Example: `+1234567890`\n\n"
                "Send /cancel to abort.",
                buttons=[[Button.text("❌ Cancel")]]
            )

        @self.client.on(events.NewMessage(pattern="👥 (Accounts|View Accounts)"))
        @admin_only
        async def accounts_button_handler(event):
            """Handle Accounts button"""
            with get_db_context() as db:
                accounts = db.query(Account).all()

            if not accounts:
                await event.respond(
                    "No accounts added yet.\n\n"
                    "Use /login to add your first Telegram account."
                )
                return

            message = "👥 **Registered Accounts**\n\n"
            for acc in accounts[:10]:
                status_emoji = {
                    AccountStatus.ACTIVE: "✅",
                    AccountStatus.INACTIVE: "⏸️",
                    AccountStatus.BANNED: "🚫",
                    AccountStatus.FLOOD_WAIT: "⏳",
                    AccountStatus.AUTH_REQUIRED: "🔑",
                }.get(acc.status, "❓")

                username_str = f"@{acc.username}" if acc.username else "No username"

                message += (
                    f"{status_emoji} **{acc.phone_number}**\n"
                    f"   └ {username_str} | "
                    f"Stories: {acc.stories_today}\n"
                )

            if len(accounts) > 10:
                message += f"\n_...and {len(accounts) - 10} more_"

            await event.respond(message)

        @self.client.on(events.NewMessage(pattern="📊 Stats"))
        @admin_only
        async def stats_button_handler(event):
            """Handle Stats button"""
            with get_db_context() as db:
                accounts_total = db.query(Account).count()
                accounts_active = db.query(Account).filter(
                    Account.status == AccountStatus.ACTIVE
                ).count()
                stories_total = db.query(Story).count()
                users_total = db.query(DiscoveredUser).count()

            message = (
                "📊 **STORYFLEET Statistics**\n\n"
                f"👥 **Accounts**: {accounts_active}/{accounts_total} active\n"
                f"📸 **Stories**: {stories_total} published\n"
                f"🔍 **Users**: {users_total} discovered"
            )

            await event.respond(message)

        @self.client.on(events.NewMessage(pattern="❓ Help"))
        @admin_only
        async def help_button_handler(event):
            """Handle Help button"""
            await event.respond(
                "📖 **STORYFLEET Commands**\n\n"
                "📱 /login - Add new Telegram account\n"
                "👥 /accounts - List all accounts\n"
                "📊 /stats - View statistics\n"
                "📤 /publish - Publish a story\n"
                "🔍 /scan - Scan for users\n"
                "🎯 /campaigns - View campaigns\n"
                "/help - Full command list"
            )

        logger.info("Event handlers registered")


async def run_bot():
    """Entry point to run the bot"""
    bot = StoryFleetBot()
    await bot.run()


if __name__ == "__main__":
    asyncio.run(run_bot())
