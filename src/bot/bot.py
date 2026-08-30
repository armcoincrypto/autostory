"""
Telegram Bot Dashboard
Alternative control interface via Telegram Bot with User Login Flow
"""
import asyncio
import re
from datetime import datetime
from typing import Optional, Dict, Any
from functools import wraps

from telethon import TelegramClient, events
from telethon.events import StopPropagation
from telethon.sessions import StringSession
from telethon.sessions import SQLiteSession
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
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from config.settings import settings
from src.core.models import Account, Story, DiscoveredUser, Campaign, AccountStatus
from src.core.database import get_db_context
try:
    from src.bot.kathleen import try_handle_kathleen_message
except ModuleNotFoundError:
    # See docs/audits/zollotex-social-agent-phase0-7-20260722T083419Z/KATHLEEN_DECISION.md:
    # no authoritative source for src/bot/kathleen/ survived, so it does not ship; policy is
    # "no hard import in entry points" (already true for web/scheduler/dexpert). This entry
    # point (`python main.py bot`) was the one remaining hard import.
    def try_handle_kathleen_message(sender_id: int, raw: str) -> str | None:
        return "Kathleen is unavailable in this build."

logger = structlog.get_logger(__name__)

# Store for pending phone login flows (user_id -> state dict)
pending_logins: Dict[int, Dict[str, Any]] = {}

# Store for pending session imports (user_id -> True when waiting for session string)
pending_session_imports: Dict[int, bool] = {}

# Store for pending scan sessions (user_id -> scan state)
pending_scans: Dict[int, Dict[str, Any]] = {}

# Store for pending publish sessions (user_id -> publish state)
pending_publishes: Dict[int, Dict[str, Any]] = {}


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

        # Use a dedicated session path under project data/ so systemd ReadWritePaths works
        # and we avoid conflicts with other runs (only one process should use this file).
        _root = Path(__file__).resolve().parents[2]
        session_dir = _root / "data" / "bot"
        session_dir.mkdir(parents=True, exist_ok=True)
        session_path = session_dir / "storyfleet_bot.session"

        self.client = TelegramClient(
            SQLiteSession(str(session_path)),
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

        @self.client.on(events.NewMessage(incoming=True))
        async def kathleen_operator_incoming(event):
            """Kathleen read-only operator (DB + dry-run plans only; no user Telethon)."""
            if event.out:
                return
            if not event.is_private:
                return
            raw = (event.raw_text or "").strip()
            if not raw or not re.match(r"(?is)^kathleen\b", raw):
                return
            sender = await event.get_sender()
            if not sender:
                return
            try:
                reply = await asyncio.to_thread(try_handle_kathleen_message, int(sender.id), raw)
            except Exception as e:
                logger.error("kathleen_handler_error", error=str(e))
                reply = "Temporary error. Check logs."
            if reply is None:
                return
            await event.respond(reply)
            raise StopPropagation

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
                    "📱 /login - Add account (phone + code)\n"
                    "📥 /import_session or /tdata - Add account from tdata session string\n"
                    "👥 /accounts - View all accounts\n"
                    "📊 /stats - View statistics\n"
                    "📤 /publish - Publish a story\n"
                    "🔍 /scan - Scan channel for users\n"
                    "🎯 /campaigns - View campaigns\n"
                    "📲 **Dashboard** (web): Scheduler, bulk zip import, full UI\n"
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
        async def cancel_handler(event):
            """Cancel ongoing operations (login, scan, session import, or publish)"""
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

            if user_id in pending_session_imports:
                del pending_session_imports[user_id]
                if not cancelled:
                    await event.respond("❌ Session import cancelled.")
                cancelled = True

            if user_id in pending_publishes:
                del pending_publishes[user_id]
                if not cancelled:
                    await event.respond("❌ Publish cancelled.")
                cancelled = True

            if not cancelled:
                await event.respond("No active operation to cancel.")

        @self.client.on(events.NewMessage(pattern="/import_session"))
        @admin_only
        async def import_session_cmd(event):
            """Import account from tdata session string."""
            pending_session_imports[event.sender_id] = True
            await event.respond(
                "📥 **Import from tdata**\n\n"
                "Paste the session string (from convert_tdata.py) in your next message.\n"
                "It usually starts with 1BQAOMTQ...\n\n"
                "Send /cancel to abort.",
                buttons=[[Button.text("❌ Cancel")]]
            )

        @self.client.on(events.NewMessage(pattern="/tdata"))
        @admin_only
        async def tdata_cmd(event):
            """Alias for /import_session"""
            pending_session_imports[event.sender_id] = True
            await event.respond(
                "📥 Paste your tdata session string in the next message.\n\nSend /cancel to abort.",
                buttons=[[Button.text("❌ Cancel")]]
            )

        @self.client.on(events.NewMessage())
        async def message_handler(event):
            """Handle messages for login flow and session import"""
            if event.text and event.text.startswith("/"):
                return

            sender = await event.get_sender()
            if not sender:
                return

            user_id = sender.id

            if user_id not in settings.bot.admin_ids:
                return

            # Handle session string import (tdata)
            if user_id in pending_session_imports:
                if event.text == "❌ Cancel":
                    del pending_session_imports[user_id]
                    await event.respond("❌ Session import cancelled.")
                    return
                session_string = (event.text or "").strip()
                if not session_string or len(session_string) < 50:
                    await event.respond("❌ Session string too short. Paste the full string from convert_tdata.py.")
                    return
                del pending_session_imports[user_id]
                await event.respond("⏳ Importing session...")
                try:
                    from src.clients.manager import client_manager
                    result = await client_manager.import_session_string(session_string, import_source="paste")
                    if result.get("success"):
                        await event.respond(
                            f"✅ **{result.get('message', 'Account imported!')}**\n\n"
                            f"Account ID: #{result.get('account_id')} | @{result.get('username', 'N/A')}"
                        )
                    else:
                        await event.respond(f"❌ **Error:** {result.get('error', 'Import failed')}")
                except Exception as e:
                    logger.error("Import session error", error=str(e))
                    await event.respond(f"❌ **Error:** {str(e)}")
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
            """Handle /scan command - Start group scanning flow"""
            sender = await event.get_sender()
            user_id = sender.id

            # Check if there's an active account with canonical session
            with get_db_context() as db:
                active_accounts = db.query(Account).filter(Account.status == AccountStatus.ACTIVE).all()
            from src.core.session_paths import account_has_canonical_session
            active_account = next((a for a in active_accounts if account_has_canonical_session(a)), None)

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
                accounts = db.query(Account).filter(Account.status == AccountStatus.ACTIVE).all()
                users_count = db.query(DiscoveredUser).filter(
                    DiscoveredUser.times_mentioned == 0
                ).count()
            from src.core.session_paths import account_has_canonical_session
            accounts = [a for a in accounts if account_has_canonical_session(a)]

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
                "account_ids": [acc.id for acc in accounts],  # Store all account IDs
                "mentions": 5,
                "caption": "",
            }

            # Build buttons - show ALL option first if multiple accounts
            buttons = []
            if len(accounts) > 1:
                buttons.append([Button.inline(f"📤 Publish to ALL {len(accounts)} Accounts", data="pub_all")])

            for acc in accounts[:10]:
                buttons.append([Button.inline(f"📱 {acc.phone_number} (@{acc.username or 'N/A'})", data=f"pub_{acc.id}")])

            buttons.append([Button.inline("❌ Cancel", data="pub_cancel")])

            await event.respond(
                "📤 **Publish Story**\n\n"
                f"📱 Active accounts: {len(accounts)}\n"
                f"👥 Users available for mention: {users_count}\n\n"
                "Select account(s) to publish from:",
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

        @self.client.on(events.CallbackQuery(pattern="pub_all"))
        @admin_only
        async def publish_all_selected(event):
            """Handle 'Publish to ALL' selection"""
            sender = await event.get_sender()
            user_id = sender.id

            # Update state with all account IDs
            if user_id not in pending_publishes:
                pending_publishes[user_id] = {}

            account_ids = pending_publishes[user_id].get("account_ids", [])
            pending_publishes[user_id]["account_id"] = "all"
            pending_publishes[user_id]["publish_to_all"] = True
            pending_publishes[user_id]["step"] = "waiting_media"

            await event.edit(
                f"📤 **Publishing to ALL {len(account_ids)} Accounts**\n\n"
                "📸 **Send the media file now:**\n"
                "• Photo (JPG, PNG)\n"
                "• Video (MP4, up to 15 seconds)\n\n"
                "The story will be published to all accounts with mentions.\n\n"
                "Send /cancel to abort."
            )

        @self.client.on(events.CallbackQuery(pattern="pub_cancel"))
        async def publish_cancel_handler(event):
            """Handle publish cancel"""
            sender = await event.get_sender()
            if sender.id in pending_publishes:
                del pending_publishes[sender.id]
            await event.edit("❌ Publishing cancelled.")

        @self.client.on(events.NewMessage(func=lambda e: e.media))
        @admin_only
        async def media_handler(event):
            """Handle media upload for story publishing"""
            sender = await event.get_sender()
            user_id = sender.id

            # Check if waiting for media
            if user_id not in pending_publishes:
                return

            if pending_publishes[user_id].get("step") != "waiting_media":
                return

            account_id = pending_publishes[user_id].get("account_id")
            if not account_id:
                return

            # Download media
            await event.respond("⏳ Downloading media...")

            import os
            os.makedirs("data/media", exist_ok=True)
            media_path = await event.download_media(file="data/media/")

            if not media_path:
                await event.respond("❌ Failed to download media. Please try again.")
                return

            # Save media path and wait for caption
            pending_publishes[user_id]["media_path"] = media_path
            pending_publishes[user_id]["step"] = "waiting_caption"

            await event.respond(
                "✅ **Media received!**\n\n"
                "📝 **Now send the caption text:**\n\n"
                "• Send your caption text\n"
                "• Or send `-` for no caption\n\n"
                "Send /cancel to abort."
            )

        @self.client.on(events.NewMessage())
        async def caption_handler(event):
            """Handle caption for story publishing"""
            # Skip commands
            if event.text and event.text.startswith("/"):
                return

            # Skip media messages
            if event.media:
                return

            sender = await event.get_sender()
            if not sender:
                return

            user_id = sender.id

            # Check admin
            if user_id not in settings.bot.admin_ids:
                return

            # Check if waiting for caption
            if user_id not in pending_publishes:
                return

            if pending_publishes[user_id].get("step") != "waiting_caption":
                return

            account_id = pending_publishes[user_id].get("account_id")
            media_path = pending_publishes[user_id].get("media_path")
            publish_to_all = pending_publishes[user_id].get("publish_to_all", False)
            account_ids = pending_publishes[user_id].get("account_ids", [])

            if not media_path:
                del pending_publishes[user_id]
                await event.respond("❌ Session expired. Please start over with /publish")
                return

            # Get caption (use empty if "-")
            caption = "" if event.text == "-" else (event.text or "")

            # Clear state
            del pending_publishes[user_id]

            try:
                from src.publisher.story_publisher import story_publisher
                import os

                if publish_to_all and account_ids:
                    # Publish to ALL accounts
                    await event.respond(
                        f"🚀 **Publishing story to {len(account_ids)} accounts...**\n\n"
                        "This may take a moment..."
                    )

                    success_count = 0
                    fail_count = 0
                    total_mentions = 0
                    results_detail = []

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
                                total_mentions += result.get("mentions_added", 0)
                                results_detail.append(f"✅ Account #{acc_id}: Published")
                            else:
                                fail_count += 1
                                results_detail.append(f"❌ Account #{acc_id}: {result.get('error', 'Failed')}")
                        except Exception as e:
                            fail_count += 1
                            results_detail.append(f"❌ Account #{acc_id}: {str(e)[:50]}")

                    # Clean up media file
                    try:
                        os.remove(media_path)
                    except:
                        pass

                    # Show results
                    details_str = "\n".join(results_detail[:10])
                    if len(results_detail) > 10:
                        details_str += f"\n...and {len(results_detail) - 10} more"

                    await event.respond(
                        f"📊 **Batch Publish Complete!**\n\n"
                        f"✅ Success: {success_count}/{len(account_ids)}\n"
                        f"❌ Failed: {fail_count}\n"
                        f"👥 Total mentions: {total_mentions}\n"
                        f"📝 Caption: {caption[:30] + '...' if len(caption) > 30 else caption or '(none)'}\n\n"
                        f"**Details:**\n{details_str}",
                        buttons=[
                            [Button.text("📤 Publish Another", resize=True)],
                            [Button.text("📊 Stats")],
                        ]
                    )
                else:
                    # Publish to single account
                    if not account_id:
                        await event.respond("❌ No account selected. Please start over with /publish")
                        return

                    await event.respond(
                        "🚀 **Publishing story...**\n\n"
                        "• Uploading media\n"
                        "• Adding mentions\n"
                        "• Publishing to story"
                    )

                    result = await story_publisher.publish_with_auto_mentions(
                        media_path=media_path,
                        caption=caption,
                        mentions_count=5,
                        account_id=account_id,
                    )

                    # Clean up media file
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
                            f"• Mentions added: {result['mentions_added']}\n"
                            f"• Caption: {caption[:50] + '...' if len(caption) > 50 else caption or '(none)'}\n\n"
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

        @self.client.on(events.NewMessage(pattern="📤 Publish Another"))
        @admin_only
        async def publish_another_handler(event):
            """Handle Publish Another button (same flow as /publish: ALL + each account)"""
            sender = await event.get_sender()
            user_id = sender.id

            with get_db_context() as db:
                accounts = db.query(Account).filter(Account.status == AccountStatus.ACTIVE).all()
                users_count = db.query(DiscoveredUser).filter(
                    DiscoveredUser.times_mentioned == 0
                ).count()
            from src.core.session_paths import account_has_canonical_session
            accounts = [a for a in accounts if account_has_canonical_session(a)]

            if not accounts:
                await event.respond("No active accounts. Use /login first.")
                return

            pending_publishes[user_id] = {
                "step": "select_account",
                "account_id": None,
                "account_ids": [acc.id for acc in accounts],
                "mentions": 5,
                "caption": "",
            }

            buttons = []
            if len(accounts) > 1:
                buttons.append([Button.inline(f"📤 Publish to ALL {len(accounts)} Accounts", data="pub_all")])
            for acc in accounts[:10]:
                buttons.append([Button.inline(f"📱 {acc.phone_number} (@{acc.username or 'N/A'})", data=f"pub_{acc.id}")])
            buttons.append([Button.inline("❌ Cancel", data="pub_cancel")])

            await event.respond(
                "📤 **Publish Story**\n\n"
                f"📱 Active accounts: {len(accounts)}\n"
                f"👥 Users available for mention: {users_count}\n\n"
                "Select account(s) to publish from:",
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

        @self.client.on(events.NewMessage(pattern="/help"))
        @admin_only
        async def help_handler(event):
            """Handle /help command"""
            await event.respond(
                "📖 **STORYFLEET Commands**\n\n"
                "**Account Management**\n"
                "📱 /login - Add account (phone + code)\n"
                "📥 /import_session or /tdata - Add account from tdata session string\n"
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
                "**Dashboard (web)**\n"
                "📲 Use the Dashboard for Scheduler (messaging to groups), bulk zip import, and full UI.\n\n"
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
            """Handle Stats button (same as /stats)"""
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
                "📲 Dashboard: Scheduler (messaging), bulk zip import\n"
                "/help - Full command list"
            )

        logger.info("Event handlers registered")


async def run_bot():
    """Entry point to run the bot"""
    bot = StoryFleetBot()
    await bot.run()


if __name__ == "__main__":
    asyncio.run(run_bot())
