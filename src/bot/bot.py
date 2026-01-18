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

# Store for pending scan sessions (user_id -> scan state)
pending_scans: Dict[int, Dict[str, Any]] = {}

# Store for pending publish sessions (user_id -> publish state)
pending_publishes: Dict[int, Dict[str, Any]] = {}

# Store for pending tdata imports (user_id -> import state)
pending_tdata_imports: Dict[int, Dict[str, Any]] = {}


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
                    existing.last_active = datetime.utcnow()
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

            # Get quick stats
            with get_db_context() as db:
                accounts_count = db.query(Account).filter(Account.status == AccountStatus.ACTIVE).count()
                users_count = db.query(DiscoveredUser).filter(
                    DiscoveredUser.times_mentioned == 0,
                    DiscoveredUser.username.isnot(None)
                ).count()

            welcome = (
                "🚀 **STORYFLEET**\n\n"
                "Multi-Account Story Publisher\n\n"
            )

            if is_admin:
                welcome += (
                    f"📊 **Status:** {accounts_count} accounts | {users_count} users ready\n\n"
                    "**Quick Actions:**\n"
                    "📤 /publish_all - Publish to ALL accounts\n"
                    "🗑 /delete_story - Delete stories\n"
                    "📱 /login - Add account\n"
                    "🔍 /scan - Scan for users\n\n"
                    "Use /help for full command list"
                )
            else:
                welcome += "⛔ You are not authorized to use this bot."

            await event.respond(
                welcome,
                buttons=[
                    [Button.text("📤 Publish All", resize=True), Button.text("🗑 Delete Stories")],
                    [Button.text("📱 Login Account"), Button.text("🔍 Scan Users")],
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
        async def cancel_handler(event):
            """Cancel ongoing operations (login or scan)"""
            sender = await event.get_sender()
            user_id = sender.id

            cancelled = False

            if user_id in pending_logins:
                old_client = pending_logins[user_id].get("client")
                if old_client:
                    try:
                        await old_client.disconnect()
                    except:
                        pass
                del pending_logins[user_id]
                await event.respond("❌ Login cancelled.")
                cancelled = True

            if user_id in pending_scans:
                del pending_scans[user_id]
                if not cancelled:
                    await event.respond("❌ Scan cancelled.")
                cancelled = True

            if not cancelled:
                await event.respond("No active operation to cancel.")

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

        @self.client.on(events.NewMessage(pattern="/status"))
        @admin_only
        async def status_handler(event):
            """Handle /status command - Detailed system health"""
            import psutil
            from datetime import datetime

            with get_db_context() as db:
                # Account health
                active = db.query(Account).filter(Account.status == AccountStatus.ACTIVE).count()
                banned = db.query(Account).filter(Account.status == AccountStatus.BANNED).count()
                flood = db.query(Account).filter(Account.status == AccountStatus.FLOOD_WAIT).count()
                auth_req = db.query(Account).filter(Account.status == AccountStatus.AUTH_REQUIRED).count()

                # Recent activity
                now = datetime.utcnow()
                from datetime import timedelta
                hour_ago = now - timedelta(hours=1)
                day_ago = now - timedelta(days=1)

                stories_hour = db.query(Story).filter(Story.published_at >= hour_ago).count()
                stories_day = db.query(Story).filter(Story.published_at >= day_ago).count()

                # Get last error
                last_account_error = db.query(Account).filter(
                    Account.last_error.isnot(None)
                ).order_by(Account.last_active.desc()).first()

                last_error_msg = "None"
                if last_account_error and last_account_error.last_error:
                    last_error_msg = last_account_error.last_error[:50] + "..."

            # System health
            try:
                cpu_percent = psutil.cpu_percent()
                memory = psutil.virtual_memory()
                disk = psutil.disk_usage('/')
                system_info = (
                    f"├ CPU: {cpu_percent}%\n"
                    f"├ RAM: {memory.percent}%\n"
                    f"└ Disk: {disk.percent}%"
                )
            except:
                system_info = "└ Unable to get system info"

            message = f"""🖥 **SYSTEM STATUS**

**Bot Status**: ✅ Online
**Uptime**: Running

**Accounts Health**
├ Active: {active} ✅
├ Banned: {banned} 🚫
├ Flood Wait: {flood} ⏳
└ Auth Required: {auth_req} 🔑

**Activity**
├ Stories (1h): {stories_hour}
└ Stories (24h): {stories_day}

**System Resources**
{system_info}

**Last Error**
└ {last_error_msg}

_Use /monitor for detailed analytics_"""

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
            """Handle /scan command - Start group scanning flow"""
            sender = await event.get_sender()
            user_id = sender.id

            # Check if there's an active account
            with get_db_context() as db:
                active_account = db.query(Account).filter(
                    Account.status == AccountStatus.ACTIVE,
                    Account.session_string.isnot(None)
                ).first()

                if not active_account:
                    await event.respond(
                        "❌ **No active account**\n\n"
                        "You need to login a Telegram account first.\n"
                        "Use /login to add an account."
                    )
                    return

            # Set scan state
            pending_scans[user_id] = {
                "step": "waiting_group",
                "groups": [],
            }

            await event.respond(
                "🔍 **Scan Group for Users**\n\n"
                "This will scan 1 year of message history and collect users.\n\n"
                "**Send the group username or link:**\n"
                "Examples:\n"
                "• `@groupname`\n"
                "• `https://t.me/groupname`\n"
                "• `https://t.me/+invitecode`\n\n"
                "Send /cancel to abort.",
                buttons=[[Button.text("❌ Cancel")]]
            )

        @self.client.on(events.NewMessage(pattern=r"^(@[\w]+|https?://t\.me/.+)$"))
        @admin_only
        async def scan_group_input_handler(event):
            """Handle group input for scanning"""
            sender = await event.get_sender()
            user_id = sender.id

            # Check if waiting for scan input
            if user_id not in pending_scans:
                return

            if pending_scans[user_id].get("step") != "waiting_group":
                return

            group = event.text.strip()

            # Clear scan state
            del pending_scans[user_id]

            # Send processing message
            progress_msg = await event.respond(
                f"🔄 **Starting scan of {group}**\n\n"
                "Scanning 1 year of messages...\n"
                "This may take several minutes for active groups."
            )

            # Progress callback function
            async def update_progress(text):
                try:
                    await progress_msg.edit(text)
                except:
                    pass

            # Run scanner
            from src.discovery.scanner import user_discovery

            try:
                result = await user_discovery.discover_from_group(
                    group,
                    days_back=365,
                    progress_callback=update_progress,
                )

                # Format result message
                if result["success"]:
                    duration = int(result.get("duration_seconds", 0))
                    mins = duration // 60
                    secs = duration % 60

                    await event.respond(
                        f"✅ **Scan Complete!**\n\n"
                        f"**Group:** {result.get('group_title', group)}\n"
                        f"**Duration:** {mins}m {secs}s\n\n"
                        f"📊 **Results:**\n"
                        f"• Messages scanned: {result['messages_scanned']:,}\n"
                        f"• Unique users found: {result['unique_users_found']:,}\n"
                        f"• **New users saved: {result['new_users_saved']:,}**\n"
                        f"• Deleted users skipped: {result['deleted_users_skipped']:,}\n"
                        f"• Bots skipped: {result['bots_skipped']:,}\n"
                        f"• Duplicates skipped: {result['duplicates_skipped']:,}\n\n"
                        f"Use /users to see available users for mention.",
                        buttons=[
                            [Button.text("🔍 Scan Another", resize=True), Button.text("👥 View Users")],
                            [Button.text("📊 Stats")],
                        ]
                    )
                else:
                    errors = "\n".join(result.get("errors", ["Unknown error"]))
                    await event.respond(
                        f"❌ **Scan Failed**\n\n"
                        f"**Group:** {group}\n"
                        f"**Errors:**\n{errors}\n\n"
                        "Make sure:\n"
                        "• The logged-in account is a member of the group\n"
                        "• The group is accessible\n"
                        "• Try with a different group"
                    )

            except Exception as e:
                logger.error("Scan error", error=str(e))
                await event.respond(
                    f"❌ **Scan Error**\n\n"
                    f"Error: {str(e)}\n\n"
                    "Please try again."
                )

        @self.client.on(events.NewMessage(pattern="🔍 Scan Another"))
        @admin_only
        async def scan_another_handler(event):
            """Handle Scan Another button"""
            sender = await event.get_sender()
            pending_scans[sender.id] = {"step": "waiting_group", "groups": []}
            await event.respond(
                "🔍 **Scan Group for Users**\n\n"
                "Send the group username or link:\n"
                "Example: `@groupname` or `https://t.me/groupname`\n\n"
                "Send /cancel to abort.",
                buttons=[[Button.text("❌ Cancel")]]
            )

        @self.client.on(events.NewMessage(pattern="👥 View Users"))
        @admin_only
        async def view_users_handler(event):
            """Handle View Users button"""
            with get_db_context() as db:
                users = db.query(DiscoveredUser).filter(
                    DiscoveredUser.times_mentioned == 0
                ).order_by(
                    DiscoveredUser.discovered_at.desc()
                ).limit(15).all()

                if not users:
                    await event.respond("No users available for mention yet.")
                    return

                message = "👥 **Users Available for Mention**\n\n"
                for user in users:
                    username = f"@{user.username}" if user.username else f"ID:{user.user_id}"
                    name = user.first_name or "N/A"
                    message += f"• {username} ({name})\n"

                total = db.query(DiscoveredUser).filter(
                    DiscoveredUser.times_mentioned == 0
                ).count()

                message += f"\n_Showing 15 of {total} available users_"

                await event.respond(message)

        @self.client.on(events.NewMessage(pattern="/publish"))
        @admin_only
        async def publish_handler(event):
            """Handle /publish command"""
            sender = await event.get_sender()
            user_id = sender.id

            with get_db_context() as db:
                accounts = db.query(Account).filter(
                    Account.status == AccountStatus.ACTIVE,
                    Account.session_string.isnot(None)
                ).all()

                users_count = db.query(DiscoveredUser).filter(
                    DiscoveredUser.times_mentioned == 0
                ).count()

                if not accounts:
                    await event.respond(
                        "❌ **No active accounts**\n\n"
                        "Use /login to add an account first."
                    )
                    return

                # Initialize publish state
                pending_publishes[user_id] = {
                    "step": "select_account",
                    "account_id": None,
                    "mentions": 5,
                    "caption": "",
                }

                buttons = [
                    [Button.inline(f"📱 {acc.phone_number}", data=f"pub_{acc.id}")]
                    for acc in accounts[:5]
                ]
                buttons.append([Button.inline("❌ Cancel", data="pub_cancel")])

                await event.respond(
                    "📤 **Publish Story**\n\n"
                    f"👥 Available users for mention: {users_count}\n\n"
                    "Select an account to publish from:",
                    buttons=buttons
                )

        @self.client.on(events.CallbackQuery(pattern=r"pub_(\d+)"))
        @admin_only
        async def publish_account_selected(event):
            """Handle account selection for publishing"""
            sender = await event.get_sender()
            user_id = sender.id

            account_id = int(event.data.decode().split("_")[1])

            # Update state
            if user_id not in pending_publishes:
                pending_publishes[user_id] = {}

            pending_publishes[user_id]["account_id"] = account_id
            pending_publishes[user_id]["step"] = "waiting_media"

            await event.edit(
                f"📤 **Publishing from Account #{account_id}**\n\n"
                "📸 **Send the media file now:**\n"
                "• Photo (JPG, PNG)\n"
                "• Video (MP4, up to 15 seconds)\n\n"
                "The story will be published with automatic user mentions.\n\n"
                "Send /cancel to abort."
            )

        @self.client.on(events.CallbackQuery(pattern="pub_cancel"))
        async def publish_cancel_handler(event):
            """Handle publish cancel"""
            sender = await event.get_sender()
            if sender.id in pending_publishes:
                del pending_publishes[sender.id]
            await event.edit("❌ Publishing cancelled.")

        @self.client.on(events.NewMessage(pattern="📤 Publish Another"))
        @admin_only
        async def publish_another_handler(event):
            """Handle Publish Another button"""
            sender = await event.get_sender()
            user_id = sender.id

            with get_db_context() as db:
                accounts = db.query(Account).filter(
                    Account.status == AccountStatus.ACTIVE,
                    Account.session_string.isnot(None)
                ).all()

                if not accounts:
                    await event.respond("No active accounts. Use /login first.")
                    return

                pending_publishes[user_id] = {"step": "select_account"}

                buttons = [
                    [Button.inline(f"📱 {acc.phone_number}", data=f"pub_{acc.id}")]
                    for acc in accounts[:5]
                ]
                buttons.append([Button.inline("❌ Cancel", data="pub_cancel")])

                await event.respond(
                    "📤 **Publish Story**\n\n"
                    "Select an account:",
                    buttons=buttons
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

        @self.client.on(events.NewMessage(pattern="/monitor"))
        @admin_only
        async def monitor_handler(event):
            """Handle /monitor command - Show detailed analytics"""
            try:
                from src.monitoring.story_monitor import story_monitor

                await event.respond("📊 Gathering analytics...")

                stats = story_monitor.get_system_stats()
                message = story_monitor.format_stats_message(stats)

                await event.respond(message)

            except Exception as e:
                logger.error("Monitor command error", error=str(e))
                await event.respond(f"❌ Error: {str(e)}")

        @self.client.on(events.NewMessage(pattern="/delete_story"))
        @admin_only
        async def delete_story_handler(event):
            """Handle /delete_story command - Delete stories from accounts"""
            from telethon.tl.functions.stories import DeleteStoriesRequest, GetAllStoriesRequest
            from telethon.tl.types import InputPeerSelf

            with get_db_context() as db:
                accounts = db.query(Account).filter(
                    Account.status == AccountStatus.ACTIVE,
                    Account.session_string.isnot(None)
                ).all()

                if not accounts:
                    await event.respond("❌ No active accounts.")
                    return

                # Build account data list
                account_data = [(acc.id, acc.phone_number, acc.session_string) for acc in accounts]

            buttons = [
                [Button.inline(f"🗑 {phone}", data=f"del_{acc_id}")]
                for acc_id, phone, _ in account_data[:5]
            ]
            buttons.append([Button.inline("🗑 Delete ALL Stories", data="del_all")])
            buttons.append([Button.inline("❌ Cancel", data="del_cancel")])

            await event.respond(
                "🗑 **Delete Stories**\n\n"
                "Select account to delete story from:",
                buttons=buttons
            )

        @self.client.on(events.CallbackQuery(pattern=r"del_(\d+)"))
        @admin_only
        async def delete_single_story(event):
            """Delete story from single account"""
            from telethon.tl.functions.stories import DeleteStoriesRequest, GetAllStoriesRequest
            from telethon.tl.types import InputPeerSelf

            account_id = int(event.data.decode().split("_")[1])

            await event.edit("🗑 Deleting story...")

            with get_db_context() as db:
                account = db.query(Account).filter(Account.id == account_id).first()
                if not account:
                    await event.edit("❌ Account not found.")
                    return
                session_string = account.session_string
                phone = account.phone_number

            try:
                client = TelegramClient(
                    StringSession(session_string),
                    settings.telegram.api_id,
                    settings.telegram.api_hash
                )
                await client.connect()

                # Get current stories
                stories = await client(GetAllStoriesRequest(next="", hidden=False, state=""))
                my_stories = []
                if hasattr(stories, 'peer_stories'):
                    for peer_story in stories.peer_stories:
                        if hasattr(peer_story, 'stories'):
                            my_stories = [s.id for s in peer_story.stories]
                            break

                if not my_stories:
                    await client.disconnect()
                    await event.edit(f"ℹ️ No active stories on {phone}")
                    return

                # Delete all stories
                await client(DeleteStoriesRequest(peer=InputPeerSelf(), id=my_stories))
                await client.disconnect()

                await event.edit(
                    f"✅ **Deleted {len(my_stories)} stories from {phone}**"
                )

            except Exception as e:
                logger.error("Delete story error", error=str(e))
                await event.edit(f"❌ Error: {str(e)}")

        @self.client.on(events.CallbackQuery(pattern="del_all"))
        @admin_only
        async def delete_all_stories(event):
            """Delete stories from ALL accounts"""
            from telethon.tl.functions.stories import DeleteStoriesRequest, GetAllStoriesRequest
            from telethon.tl.types import InputPeerSelf

            await event.edit("🗑 Deleting stories from all accounts...")

            with get_db_context() as db:
                accounts = db.query(Account).filter(
                    Account.status == AccountStatus.ACTIVE,
                    Account.session_string.isnot(None)
                ).all()
                account_data = [(acc.id, acc.phone_number, acc.session_string) for acc in accounts]

            results = []
            for acc_id, phone, session_string in account_data:
                try:
                    client = TelegramClient(
                        StringSession(session_string),
                        settings.telegram.api_id,
                        settings.telegram.api_hash
                    )
                    await client.connect()

                    stories = await client(GetAllStoriesRequest(next="", hidden=False, state=""))
                    my_stories = []
                    if hasattr(stories, 'peer_stories'):
                        for peer_story in stories.peer_stories:
                            if hasattr(peer_story, 'stories'):
                                my_stories = [s.id for s in peer_story.stories]
                                break

                    if my_stories:
                        await client(DeleteStoriesRequest(peer=InputPeerSelf(), id=my_stories))
                        results.append(f"✅ {phone}: {len(my_stories)} deleted")
                    else:
                        results.append(f"ℹ️ {phone}: no stories")

                    await client.disconnect()

                except Exception as e:
                    results.append(f"❌ {phone}: {str(e)[:30]}")

            await event.edit(
                "🗑 **Delete Results**\n\n" + "\n".join(results)
            )

        @self.client.on(events.CallbackQuery(pattern="del_cancel"))
        async def delete_cancel_handler(event):
            """Handle delete cancel"""
            await event.edit("❌ Cancelled.")

        @self.client.on(events.NewMessage(pattern="/tdata"))
        @admin_only
        async def tdata_handler(event):
            """Handle /tdata command - Smart tdata import"""
            args = event.text.split(maxsplit=1)

            if len(args) < 2:
                await event.respond(
                    "📁 **SMART TDATA IMPORT**\n\n"
                    "Automatically finds ALL Telegram Desktop sessions.\n\n"
                    "**Usage:**\n"
                    "`/tdata /path/to/folder`\n\n"
                    "**Example:**\n"
                    "`/tdata /root/accounts`\n"
                    "`/tdata /tmp/tdata_backup`\n\n"
                    "🔍 **Smart Search finds:**\n"
                    "• `key_datas` files (anywhere)\n"
                    "• `tdata` folders (any depth)\n"
                    "• Session files (D877F783...)\n"
                    "• Phone number folders (+123...)\n\n"
                    "Just point to ANY folder - it searches recursively!"
                )
                return

            tdata_path = args[1].strip()

            progress_msg = await event.respond(
                f"🔍 **Smart scanning:** `{tdata_path}`\n\n"
                "Searching recursively for tdata structures..."
            )

            try:
                import sys
                sys.path.insert(0, '/opt/autostory')
                from tools.tdata_analyzer import TdataAnalyzer

                analyzer = TdataAnalyzer(tdata_path)
                accounts = analyzer.discover_accounts()

                # Build detailed report
                valid_accounts = [a for a in accounts if a.is_valid]
                invalid_accounts = [a for a in accounts if not a.is_valid]

                report_lines = [
                    "📊 **TDATA SCAN RESULTS**\n",
                    f"📁 Path: `{tdata_path}`",
                    f"🔍 Search: Recursive (all depths)\n",
                    f"**Found:** {len(accounts)} tdata structure(s)",
                    f"✅ Valid: {len(valid_accounts)}",
                    f"❌ Invalid: {len(invalid_accounts)}\n",
                ]

                if valid_accounts:
                    report_lines.append("**✅ Valid Accounts:**")
                    for acc in valid_accounts[:15]:
                        phone = acc.phone_number or "No phone"
                        size_kb = round(acc.total_size / 1024, 1)
                        report_lines.append(f"• `{acc.folder_name}` - {phone} ({size_kb}KB)")

                    if len(valid_accounts) > 15:
                        report_lines.append(f"  _...and {len(valid_accounts) - 15} more_")

                if invalid_accounts:
                    report_lines.append("\n**❌ Invalid (skipped):**")
                    for acc in invalid_accounts[:5]:
                        error = acc.validation_error or "Unknown error"
                        report_lines.append(f"• `{acc.folder_name}`: {error[:40]}")

                await progress_msg.edit("\n".join(report_lines))

                # If valid accounts found, offer to import
                if valid_accounts:
                    pending_tdata_imports[event.sender_id] = {
                        'accounts': valid_accounts,
                        'path': tdata_path,
                    }

                    # List phones for quick login
                    phones_with_numbers = [a for a in valid_accounts if a.phone_number]

                    if phones_with_numbers:
                        phones_list = "\n".join([
                            f"• `{a.phone_number}`"
                            for a in phones_with_numbers[:10]
                        ])

                        await event.respond(
                            f"📱 **{len(phones_with_numbers)} accounts with phone numbers:**\n\n"
                            f"{phones_list}\n\n"
                            "Click **Start Bulk Login** to login all accounts sequentially.\n"
                            "Or use `/login` manually with each phone.",
                            buttons=[
                                [Button.inline(f"📱 Start Bulk Login ({len(phones_with_numbers)})", data="tdata_bulk_login")],
                                [Button.inline("❌ Cancel", data="tdata_cancel")]
                            ]
                        )
                    else:
                        await event.respond(
                            "⚠️ Valid tdata found but no phone numbers extracted.\n\n"
                            "The tdata files don't contain readable phone numbers.\n"
                            "You'll need to login manually with `/login`."
                        )
                else:
                    await event.respond(
                        "❌ **No valid tdata found**\n\n"
                        "Make sure the folder contains:\n"
                        "• `key_datas` or `key_data` file\n"
                        "• `map` file\n"
                        "• Session files (D877F783... pattern)"
                    )

            except FileNotFoundError:
                await progress_msg.edit(f"❌ Path not found: `{tdata_path}`")
            except PermissionError:
                await progress_msg.edit(f"❌ Permission denied: `{tdata_path}`")
            except Exception as e:
                logger.error("Tdata analysis error", error=str(e))
                await progress_msg.edit(f"❌ Error: {str(e)}")

        @self.client.on(events.CallbackQuery(pattern="tdata_bulk_login"))
        @admin_only
        async def tdata_bulk_login_handler(event):
            """Start bulk login for tdata accounts"""
            sender = await event.get_sender()
            user_id = sender.id

            if user_id not in pending_tdata_imports:
                await event.edit("❌ No tdata accounts pending. Use /tdata first.")
                return

            accounts = pending_tdata_imports[user_id]['accounts']

            # Find accounts with phone numbers
            accounts_with_phone = [a for a in accounts if a.phone_number]

            if not accounts_with_phone:
                await event.edit(
                    "❌ No phone numbers found in tdata.\n\n"
                    "Please use /login manually with each phone number."
                )
                return

            # Start login queue
            pending_tdata_imports[user_id]['login_queue'] = accounts_with_phone.copy()
            pending_tdata_imports[user_id]['current_index'] = 0
            pending_tdata_imports[user_id]['results'] = []

            # Start first login
            first_account = accounts_with_phone[0]
            phone = first_account.phone_number

            await event.edit(
                f"📱 **Bulk Login Progress: 1/{len(accounts_with_phone)}**\n\n"
                f"Starting login for: `{phone}`\n\n"
                "A verification code will be sent to this number.\n"
                "Please enter the code when received."
            )

            # Start login process
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
                    "is_tdata_bulk": True,
                }

                await self.client.send_message(
                    event.chat_id,
                    f"✅ Code sent to `{phone}`\n\n"
                    "Enter the verification code:"
                )

            except Exception as e:
                await self.client.send_message(
                    event.chat_id,
                    f"❌ Failed to send code to {phone}: {str(e)}"
                )

        @self.client.on(events.CallbackQuery(pattern="tdata_cancel"))
        async def tdata_cancel_handler(event):
            """Cancel tdata import"""
            sender = await event.get_sender()
            if sender.id in pending_tdata_imports:
                del pending_tdata_imports[sender.id]
            await event.edit("❌ Tdata import cancelled.")

        @self.client.on(events.NewMessage(pattern="/publish_all"))
        @admin_only
        async def publish_all_handler(event):
            """Handle /publish_all command - Publish to ALL accounts"""
            sender = await event.get_sender()
            user_id = sender.id

            with get_db_context() as db:
                accounts = db.query(Account).filter(
                    Account.status == AccountStatus.ACTIVE,
                    Account.session_string.isnot(None)
                ).all()

                users_count = db.query(DiscoveredUser).filter(
                    DiscoveredUser.times_mentioned == 0,
                    DiscoveredUser.username.isnot(None)
                ).count()

                if not accounts:
                    await event.respond(
                        "❌ **No active accounts**\n\n"
                        "Use /login to add accounts first."
                    )
                    return

                account_count = len(accounts)

            pending_publishes[user_id] = {
                "step": "waiting_caption_all",
                "mode": "all",
                "account_count": account_count,
            }

            await event.respond(
                f"📤 **Publish to ALL {account_count} Accounts**\n\n"
                f"👥 Users available for mention: {users_count}\n\n"
                "📝 **Send the caption text** (or send `-` for no caption):\n\n"
                "Example: `Check out our new product!`\n\n"
                "Send /cancel to abort.",
                buttons=[[Button.text("❌ Cancel")]]
            )

        @self.client.on(events.NewMessage(func=lambda e: e.text and not e.text.startswith("/") and not e.media))
        @admin_only
        async def caption_handler(event):
            """Handle caption input for publish_all"""
            sender = await event.get_sender()
            user_id = sender.id

            if user_id not in pending_publishes:
                return

            state = pending_publishes[user_id]

            if state.get("step") == "waiting_caption_all":
                caption = event.text.strip()
                if caption == "-":
                    caption = ""

                pending_publishes[user_id]["caption"] = caption
                pending_publishes[user_id]["step"] = "waiting_media_all"

                await event.respond(
                    f"✅ Caption set: `{caption if caption else '(no caption)'}`\n\n"
                    "📸 **Now send the media file:**\n"
                    "• Photo (JPG, PNG)\n"
                    "• Video (MP4, up to 15 seconds)\n\n"
                    "Send /cancel to abort."
                )

        @self.client.on(events.NewMessage(func=lambda e: e.media))
        @admin_only
        async def media_handler_all(event):
            """Handle media upload for story publishing (single or all)"""
            sender = await event.get_sender()
            user_id = sender.id

            if user_id not in pending_publishes:
                return

            state = pending_publishes[user_id]
            step = state.get("step")

            # Handle publish_all flow
            if step == "waiting_media_all":
                caption = state.get("caption", "")
                del pending_publishes[user_id]

                await event.respond("⏳ Downloading media...")

                import os
                os.makedirs("data/media", exist_ok=True)
                media_path = await event.download_media(file="data/media/")

                if not media_path:
                    await event.respond("❌ Failed to download media.")
                    return

                # Get all active accounts
                with get_db_context() as db:
                    accounts = db.query(Account).filter(
                        Account.status == AccountStatus.ACTIVE,
                        Account.session_string.isnot(None)
                    ).all()
                    account_ids = [acc.id for acc in accounts]

                await event.respond(
                    f"🚀 **Publishing to {len(account_ids)} accounts...**\n\n"
                    "This may take a moment..."
                )

                from src.publisher.story_publisher import story_publisher

                results = []
                success_count = 0

                for acc_id in account_ids:
                    try:
                        result = await story_publisher.publish_with_auto_mentions(
                            media_path=media_path,
                            caption=caption,
                            mentions_count=5,
                            account_id=acc_id,
                        )

                        if result["success"]:
                            success_count += 1
                            results.append(f"✅ Account #{acc_id}: Story {result.get('story_id')} ({result['mentions_added']} mentions)")
                        else:
                            results.append(f"❌ Account #{acc_id}: {result.get('error', 'Failed')[:40]}")

                    except Exception as e:
                        results.append(f"❌ Account #{acc_id}: {str(e)[:40]}")

                # Clean up
                try:
                    os.remove(media_path)
                except:
                    pass

                await event.respond(
                    f"📊 **Publish Results**\n\n"
                    f"✅ Success: {success_count}/{len(account_ids)}\n\n"
                    + "\n".join(results[:10]) +
                    ("\n..." if len(results) > 10 else ""),
                    buttons=[
                        [Button.text("📤 Publish Again", resize=True)],
                        [Button.text("🗑 Delete All Stories")],
                        [Button.text("📊 Stats")],
                    ]
                )
                return

            # Handle single account publish flow (original)
            if step == "waiting_media":
                account_id = state.get("account_id")
                if not account_id:
                    return

                del pending_publishes[user_id]

                await event.respond("⏳ Downloading media...")

                import os
                os.makedirs("data/media", exist_ok=True)
                media_path = await event.download_media(file="data/media/")

                if not media_path:
                    await event.respond("❌ Failed to download media. Please try again.")
                    return

                await event.respond(
                    "🚀 **Publishing story...**\n\n"
                    "• Uploading media\n"
                    "• Adding mentions\n"
                    "• Publishing to story"
                )

                try:
                    from src.publisher.story_publisher import story_publisher

                    result = await story_publisher.publish_with_auto_mentions(
                        media_path=media_path,
                        caption="",
                        mentions_count=5,
                        account_id=account_id,
                    )

                    try:
                        os.remove(media_path)
                    except:
                        pass

                    if result["success"]:
                        await event.respond(
                            "✅ **Story Published Successfully!**\n\n"
                            f"📊 **Details:**\n"
                            f"• Account: #{result['account_id']}\n"
                            f"• Story ID: {result.get('story_id', 'N/A')}\n"
                            f"• Mentions added: {result['mentions_added']}\n\n"
                            "The story is now live!",
                            buttons=[
                                [Button.text("📤 Publish Another", resize=True)],
                                [Button.text("📊 Stats")],
                            ]
                        )
                    else:
                        await event.respond(
                            f"❌ **Publish Failed**\n\n"
                            f"Error: {result.get('error', 'Unknown error')}\n\n"
                            "Please try again."
                        )

                except Exception as e:
                    logger.error("Publish error", error=str(e))
                    await event.respond(
                        f"❌ **Error publishing story**\n\n"
                        f"{str(e)}\n\n"
                        "Please try again."
                    )

        @self.client.on(events.NewMessage(pattern="🗑 Delete All Stories"))
        @admin_only
        async def delete_all_button_handler(event):
            """Handle Delete All Stories button"""
            from telethon.tl.functions.stories import DeleteStoriesRequest, GetAllStoriesRequest
            from telethon.tl.types import InputPeerSelf

            await event.respond("🗑 Deleting stories from all accounts...")

            with get_db_context() as db:
                accounts = db.query(Account).filter(
                    Account.status == AccountStatus.ACTIVE,
                    Account.session_string.isnot(None)
                ).all()
                account_data = [(acc.id, acc.phone_number, acc.session_string) for acc in accounts]

            results = []
            for acc_id, phone, session_string in account_data:
                try:
                    client = TelegramClient(
                        StringSession(session_string),
                        settings.telegram.api_id,
                        settings.telegram.api_hash
                    )
                    await client.connect()

                    stories = await client(GetAllStoriesRequest(next="", hidden=False, state=""))
                    my_stories = []
                    if hasattr(stories, 'peer_stories'):
                        for peer_story in stories.peer_stories:
                            if hasattr(peer_story, 'stories'):
                                my_stories = [s.id for s in peer_story.stories]
                                break

                    if my_stories:
                        await client(DeleteStoriesRequest(peer=InputPeerSelf(), id=my_stories))
                        results.append(f"✅ {phone}: {len(my_stories)} deleted")
                    else:
                        results.append(f"ℹ️ {phone}: no stories")

                    await client.disconnect()

                except Exception as e:
                    results.append(f"❌ {phone}: {str(e)[:30]}")

            await event.respond(
                "🗑 **Delete Results**\n\n" + "\n".join(results)
            )

        @self.client.on(events.NewMessage(pattern="📤 Publish Again"))
        @admin_only
        async def publish_again_handler(event):
            """Handle Publish Again button"""
            sender = await event.get_sender()
            user_id = sender.id

            with get_db_context() as db:
                accounts = db.query(Account).filter(
                    Account.status == AccountStatus.ACTIVE,
                    Account.session_string.isnot(None)
                ).all()
                account_count = len(accounts)
                users_count = db.query(DiscoveredUser).filter(
                    DiscoveredUser.times_mentioned == 0,
                    DiscoveredUser.username.isnot(None)
                ).count()

            pending_publishes[user_id] = {
                "step": "waiting_caption_all",
                "mode": "all",
                "account_count": account_count,
            }

            await event.respond(
                f"📤 **Publish to ALL {account_count} Accounts**\n\n"
                f"👥 Users available for mention: {users_count}\n\n"
                "📝 **Send the caption text** (or send `-` for no caption):\n\n"
                "Send /cancel to abort.",
                buttons=[[Button.text("❌ Cancel")]]
            )

        @self.client.on(events.NewMessage(pattern="/help"))
        @admin_only
        async def help_handler(event):
            """Handle /help command"""
            await event.respond(
                "📖 **STORYFLEET Commands**\n\n"
                "**Account Management**\n"
                "📱 /login - Add new Telegram account\n"
                "📁 /tdata - Import tdata sessions (bulk)\n"
                "👥 /accounts - List all accounts\n"
                "/cancel - Cancel current operation\n\n"
                "**Statistics & Monitoring**\n"
                "📊 /stats - View overall statistics\n"
                "📈 /monitor - Detailed analytics dashboard\n"
                "🖥 /status - System health check\n\n"
                "**Stories**\n"
                "📤 /publish - Publish story (single account)\n"
                "📤 /publish_all - Publish to ALL accounts\n"
                "🗑 /delete_story - Delete stories from accounts\n\n"
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
                "**📤 Publishing:**\n"
                "/publish_all - Publish to ALL accounts\n"
                "/publish - Publish to single account\n"
                "/delete_story - Delete stories\n\n"
                "**📱 Account Management:**\n"
                "/login - Add new Telegram account\n"
                "/tdata - Import tdata (Telegram Desktop) sessions\n"
                "/accounts - View all accounts\n"
                "/cancel - Cancel current operation\n\n"
                "**🔍 Discovery:**\n"
                "/scan - Scan channel for users\n"
                "/users - View available users\n\n"
                "**📊 Monitoring:**\n"
                "/stats - View statistics\n"
                "/monitor - Detailed analytics\n"
                "/status - System health check\n\n"
                "**🎯 Campaigns:**\n"
                "/campaigns - View campaigns\n\n"
                "**Other:**\n"
                "/start - Main menu\n"
                "/help - This help message"
            )

        @self.client.on(events.NewMessage(pattern="📤 Publish All"))
        @admin_only
        async def publish_all_button_handler(event):
            """Handle Publish All button"""
            sender = await event.get_sender()
            user_id = sender.id

            with get_db_context() as db:
                accounts = db.query(Account).filter(
                    Account.status == AccountStatus.ACTIVE,
                    Account.session_string.isnot(None)
                ).all()
                account_count = len(accounts)
                users_count = db.query(DiscoveredUser).filter(
                    DiscoveredUser.times_mentioned == 0,
                    DiscoveredUser.username.isnot(None)
                ).count()

            if not accounts:
                await event.respond("❌ No active accounts. Use /login first.")
                return

            pending_publishes[user_id] = {
                "step": "waiting_caption_all",
                "mode": "all",
                "account_count": account_count,
            }

            await event.respond(
                f"📤 **Publish to ALL {account_count} Accounts**\n\n"
                f"👥 Users available for mention: {users_count}\n\n"
                "📝 **Send the caption text** (or send `-` for no caption):\n\n"
                "Send /cancel to abort.",
                buttons=[[Button.text("❌ Cancel")]]
            )

        @self.client.on(events.NewMessage(pattern="🗑 Delete Stories"))
        @admin_only
        async def delete_stories_button_handler(event):
            """Handle Delete Stories button"""
            from telethon.tl.functions.stories import DeleteStoriesRequest, GetAllStoriesRequest
            from telethon.tl.types import InputPeerSelf

            with get_db_context() as db:
                accounts = db.query(Account).filter(
                    Account.status == AccountStatus.ACTIVE,
                    Account.session_string.isnot(None)
                ).all()

                if not accounts:
                    await event.respond("❌ No active accounts.")
                    return

                account_data = [(acc.id, acc.phone_number, acc.session_string) for acc in accounts]

            buttons = [
                [Button.inline(f"🗑 {phone}", data=f"del_{acc_id}")]
                for acc_id, phone, _ in account_data[:5]
            ]
            buttons.append([Button.inline("🗑 Delete ALL Stories", data="del_all")])
            buttons.append([Button.inline("❌ Cancel", data="del_cancel")])

            await event.respond(
                "🗑 **Delete Stories**\n\n"
                "Select account to delete story from:",
                buttons=buttons
            )

        @self.client.on(events.NewMessage(pattern="🔍 Scan Users"))
        @admin_only
        async def scan_users_button_handler(event):
            """Handle Scan Users button"""
            sender = await event.get_sender()
            user_id = sender.id

            with get_db_context() as db:
                active_account = db.query(Account).filter(
                    Account.status == AccountStatus.ACTIVE,
                    Account.session_string.isnot(None)
                ).first()

                if not active_account:
                    await event.respond(
                        "❌ **No active account**\n\n"
                        "You need to login a Telegram account first.\n"
                        "Use /login to add an account."
                    )
                    return

            pending_scans[user_id] = {
                "step": "waiting_group",
                "groups": [],
            }

            await event.respond(
                "🔍 **Scan Group for Users**\n\n"
                "Send the group username or link:\n"
                "• `@groupname`\n"
                "• `https://t.me/groupname`\n\n"
                "Send /cancel to abort.",
                buttons=[[Button.text("❌ Cancel")]]
            )

        logger.info("Event handlers registered")


async def run_bot():
    """Entry point to run the bot"""
    bot = StoryFleetBot()
    await bot.run()


if __name__ == "__main__":
    asyncio.run(run_bot())
