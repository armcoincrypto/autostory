"""
Telegram Client Manager - Multi-Account Orchestration
Handles concurrent user sessions using Telethon
"""
import asyncio
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, List, Callable, Any

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    FloodWaitError,
    AuthKeyError,
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneNumberBannedError,
)
import structlog

import sys
sys.path.insert(0, '/home/user/autostory')
from config.settings import settings
from src.core.models import Account, AccountStatus
from src.core.database import get_db_context
from .rate_limiter import RateLimiter

logger = structlog.get_logger(__name__)


class TelegramClientWrapper:
    """Wrapper around Telethon client with additional functionality"""

    def __init__(
        self,
        account: Account,
        client: TelegramClient,
        rate_limiter: RateLimiter
    ):
        self.account = account
        self.client = client
        self.rate_limiter = rate_limiter
        self.is_connected = False
        self._lock = asyncio.Lock()

    async def connect(self) -> bool:
        """Connect to Telegram"""
        async with self._lock:
            try:
                await self.client.connect()
                if await self.client.is_user_authorized():
                    self.is_connected = True
                    me = await self.client.get_me()
                    logger.info(
                        "Client connected",
                        phone=self.account.phone_number,
                        user_id=me.id,
                        username=me.username
                    )
                    return True
                else:
                    logger.warning(
                        "Client not authorized",
                        phone=self.account.phone_number
                    )
                    return False
            except Exception as e:
                logger.error(
                    "Connection failed",
                    phone=self.account.phone_number,
                    error=str(e)
                )
                return False

    async def disconnect(self) -> None:
        """Disconnect from Telegram"""
        async with self._lock:
            if self.client.is_connected():
                await self.client.disconnect()
                self.is_connected = False
                logger.info("Client disconnected", phone=self.account.phone_number)

    async def execute(self, coro_func: Callable, *args, **kwargs) -> Any:
        """Execute a coroutine with rate limiting"""
        await self.rate_limiter.wait(self.account.id)

        try:
            result = await coro_func(*args, **kwargs)
            self.rate_limiter.record_success(self.account.id)
            return result
        except FloodWaitError as e:
            logger.warning(
                "Flood wait error",
                phone=self.account.phone_number,
                seconds=e.seconds
            )
            self.rate_limiter.record_flood_wait(self.account.id, e.seconds)
            raise
        except Exception as e:
            self.rate_limiter.record_error(self.account.id)
            raise


class ClientManager:
    """
    Manages multiple Telegram client connections
    Provides centralized control over all user accounts
    """

    def __init__(self):
        self._clients: Dict[int, TelegramClientWrapper] = {}
        self._rate_limiter = RateLimiter()
        self._lock = asyncio.Lock()
        self._session_dir = Path(settings.storage.sessions_dir)
        self._session_dir.mkdir(parents=True, exist_ok=True)

    async def initialize(self) -> None:
        """Initialize all active accounts from database"""
        with get_db_context() as db:
            accounts = db.query(Account).filter(
                Account.status.in_([AccountStatus.ACTIVE, AccountStatus.INACTIVE])
            ).all()

            for account in accounts:
                await self.add_account(account)

        logger.info("Client manager initialized", total_clients=len(self._clients))

    async def add_account(self, account: Account) -> Optional[TelegramClientWrapper]:
        """Add a new account to the manager"""
        async with self._lock:
            if account.id in self._clients:
                return self._clients[account.id]

            try:
                # Create session from string or file
                if account.session_string:
                    session = StringSession(account.session_string)
                else:
                    session = StringSession()

                # Create Telethon client
                client = TelegramClient(
                    session,
                    settings.telegram.api_id,
                    settings.telegram.api_hash,
                    proxy=account.proxy_config if account.proxy_config else None,
                    device_model="STORYFLEET",
                    app_version="1.0.0",
                    system_version="Linux",
                    lang_code="en",
                )

                wrapper = TelegramClientWrapper(account, client, self._rate_limiter)
                self._clients[account.id] = wrapper

                logger.info("Account added", account_id=account.id, phone=account.phone_number)
                return wrapper

            except Exception as e:
                logger.error("Failed to add account", account_id=account.id, error=str(e))
                return None

    async def remove_account(self, account_id: int) -> bool:
        """Remove an account from the manager"""
        async with self._lock:
            if account_id in self._clients:
                wrapper = self._clients[account_id]
                await wrapper.disconnect()
                del self._clients[account_id]
                logger.info("Account removed", account_id=account_id)
                return True
            return False

    async def get_client(self, account_id: int) -> Optional[TelegramClientWrapper]:
        """Get a client by account ID"""
        return self._clients.get(account_id)

    async def get_available_clients(self) -> List[TelegramClientWrapper]:
        """Get all available (connected and not rate-limited) clients"""
        available = []
        for wrapper in self._clients.values():
            if wrapper.is_connected and not self._rate_limiter.is_blocked(wrapper.account.id):
                available.append(wrapper)
        return available

    async def connect_all(self) -> Dict[int, bool]:
        """Connect all accounts"""
        results = {}
        tasks = []

        for account_id, wrapper in self._clients.items():
            tasks.append((account_id, wrapper.connect()))

        for account_id, task in tasks:
            try:
                results[account_id] = await task
            except Exception as e:
                results[account_id] = False
                logger.error("Connect failed", account_id=account_id, error=str(e))

        return results

    async def disconnect_all(self) -> None:
        """Disconnect all accounts"""
        for wrapper in self._clients.values():
            await wrapper.disconnect()
        logger.info("All clients disconnected")

    async def get_status(self) -> Dict[str, Any]:
        """Get status of all managed clients"""
        status = {
            "total_accounts": len(self._clients),
            "connected": 0,
            "rate_limited": 0,
            "accounts": []
        }

        for account_id, wrapper in self._clients.items():
            account_status = {
                "id": account_id,
                "phone": wrapper.account.phone_number,
                "connected": wrapper.is_connected,
                "rate_limited": self._rate_limiter.is_blocked(account_id),
            }

            if wrapper.is_connected:
                status["connected"] += 1
            if self._rate_limiter.is_blocked(account_id):
                status["rate_limited"] += 1

            status["accounts"].append(account_status)

        return status

    async def start_phone_auth(self, phone_number: str) -> Dict[str, Any]:
        """Start phone authentication for a new account"""
        # Create temporary client for auth
        session = StringSession()
        client = TelegramClient(
            session,
            settings.telegram.api_id,
            settings.telegram.api_hash,
        )

        try:
            await client.connect()
            sent_code = await client.send_code_request(phone_number)

            return {
                "success": True,
                "phone_number": phone_number,
                "phone_code_hash": sent_code.phone_code_hash,
                "session_string": session.save(),
                "message": f"Code sent to {phone_number}"
            }
        except PhoneNumberBannedError:
            return {"success": False, "error": "Phone number is banned"}
        except Exception as e:
            return {"success": False, "error": str(e)}
        finally:
            await client.disconnect()

    async def complete_phone_auth(
        self,
        phone_number: str,
        code: str,
        phone_code_hash: str,
        session_string: str,
        password: Optional[str] = None
    ) -> Dict[str, Any]:
        """Complete phone authentication with code"""
        session = StringSession(session_string)
        client = TelegramClient(
            session,
            settings.telegram.api_id,
            settings.telegram.api_hash,
        )

        try:
            await client.connect()

            try:
                await client.sign_in(phone_number, code, phone_code_hash=phone_code_hash)
            except SessionPasswordNeededError:
                if password:
                    await client.sign_in(password=password)
                else:
                    return {
                        "success": False,
                        "needs_password": True,
                        "session_string": session.save(),
                        "message": "2FA password required"
                    }

            me = await client.get_me()

            # Save to database
            with get_db_context() as db:
                account = Account(
                    phone_number=phone_number,
                    session_string=session.save(),
                    user_id=me.id,
                    username=me.username,
                    first_name=me.first_name,
                    last_name=me.last_name,
                    status=AccountStatus.ACTIVE,
                    last_active=datetime.utcnow(),
                )
                db.add(account)
                db.commit()
                db.refresh(account)

                # Add to manager
                await self.add_account(account)

                return {
                    "success": True,
                    "account_id": account.id,
                    "user_id": me.id,
                    "username": me.username,
                    "message": f"Successfully logged in as {me.first_name}"
                }

        except PhoneCodeInvalidError:
            return {"success": False, "error": "Invalid code"}
        except Exception as e:
            return {"success": False, "error": str(e)}
        finally:
            await client.disconnect()

    async def import_session_string(self, session_string: str) -> Dict[str, Any]:
        """Import an account from a Telethon StringSession string."""
        session = StringSession(session_string)
        client = TelegramClient(
            session,
            settings.telegram.api_id,
            settings.telegram.api_hash,
        )
        try:
            await client.connect()
            if not await client.is_user_authorized():
                return {"success": False, "error": "Session is not authorized"}

            me = await client.get_me()
            phone = me.phone or str(me.id)

            with get_db_context() as db:
                existing = db.query(Account).filter(
                    Account.phone_number == phone
                ).first()
                if not existing and me.id:
                    existing = db.query(Account).filter(
                        Account.user_id == me.id
                    ).first()

                if existing:
                    existing.session_string = session_string
                    existing.status = AccountStatus.ACTIVE
                    existing.user_id = me.id
                    existing.username = me.username
                    existing.first_name = me.first_name
                    existing.last_name = me.last_name
                    existing.last_active = datetime.utcnow()
                    db.commit()
                    db.refresh(existing)
                    await self.add_account(existing)
                    return {
                        "success": True,
                        "account_id": existing.id,
                        "user_id": me.id,
                        "username": me.username,
                        "phone_number": phone,
                        "message": f"Updated existing account {me.first_name}",
                    }
                else:
                    account = Account(
                        phone_number=phone,
                        session_string=session_string,
                        user_id=me.id,
                        username=me.username,
                        first_name=me.first_name,
                        last_name=me.last_name,
                        status=AccountStatus.ACTIVE,
                        last_active=datetime.utcnow(),
                    )
                    db.add(account)
                    db.commit()
                    db.refresh(account)
                    await self.add_account(account)
                    return {
                        "success": True,
                        "account_id": account.id,
                        "user_id": me.id,
                        "username": me.username,
                        "phone_number": phone,
                        "message": f"Imported account {me.first_name}",
                    }
        except Exception as e:
            return {"success": False, "error": str(e)}
        finally:
            await client.disconnect()

    async def get_dialogs(self, account_id: int, limit: int = 200) -> Dict[str, Any]:
        """Get dialogs (chats/channels) for an account."""
        wrapper = self._clients.get(account_id)
        if not wrapper:
            return {"error": "Account not in manager", "dialogs": []}
        if not wrapper.is_connected:
            connected = await wrapper.connect()
            if not connected:
                return {"error": "Could not connect account", "dialogs": []}
        try:
            dialogs = []
            async for dialog in wrapper.client.iter_dialogs(limit=limit):
                entity = dialog.entity
                d_type = "private"
                if hasattr(entity, "broadcast") and entity.broadcast:
                    d_type = "channel"
                elif hasattr(entity, "megagroup") and entity.megagroup:
                    d_type = "supergroup"
                elif dialog.is_group:
                    d_type = "group"
                dialogs.append({
                    "id": dialog.id,
                    "name": dialog.name,
                    "type": d_type,
                    "unread": dialog.unread_count,
                    "username": getattr(entity, "username", None),
                })
            return {"dialogs": dialogs, "total": len(dialogs)}
        except Exception as e:
            logger.error("get_dialogs failed", account_id=account_id, error=str(e))
            return {"error": str(e), "dialogs": []}


# ---------------------------------------------------------------------------
# QR Login — module-level helpers (sync wrappers around async Telethon flow)
# ---------------------------------------------------------------------------

_qr_sessions: Dict[str, Dict] = {}
_qr_lock = threading.Lock()


def _qr_login_thread(token: str) -> None:
    """Background thread: runs the full QR login coroutine and updates state."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _run() -> None:
        with _qr_lock:
            state = _qr_sessions.get(token)
        if not state:
            return

        client = TelegramClient(
            StringSession(),
            settings.telegram.api_id,
            settings.telegram.api_hash,
        )
        state["client"] = client
        try:
            await client.connect()
            qr = await client.qr_login()
            state["url"] = qr.url
            state["status"] = "waiting"

            me = await qr.wait(60 * 5)  # wait up to 5 minutes
            session_str = client.session.save()

            with get_db_context() as db:
                existing = db.query(Account).filter(
                    Account.user_id == me.id
                ).first()
                if existing:
                    existing.session_string = session_str
                    existing.status = AccountStatus.ACTIVE
                    db.commit()
                    account_id = existing.id
                else:
                    account = Account(
                        phone_number=me.phone or str(me.id),
                        session_string=session_str,
                        user_id=me.id,
                        username=me.username,
                        first_name=me.first_name,
                        last_name=me.last_name,
                        status=AccountStatus.ACTIVE,
                        last_active=datetime.utcnow(),
                    )
                    db.add(account)
                    db.commit()
                    db.refresh(account)
                    account_id = account.id

            state["account_id"] = account_id
            state["status"] = "completed"
        except asyncio.TimeoutError:
            state["status"] = "expired"
            state["error"] = "QR code expired — not scanned within 5 minutes"
        except Exception as e:
            state["status"] = "error"
            state["error"] = str(e)
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    loop.run_until_complete(_run())
    loop.close()


def start_qr_login() -> Dict[str, Any]:
    """Start a QR code login session. Returns URL + polling token."""
    token = secrets.token_urlsafe(16)
    state: Dict[str, Any] = {
        "status": "starting",
        "url": None,
        "account_id": None,
        "error": None,
        "created_at": datetime.utcnow(),
    }
    with _qr_lock:
        _qr_sessions[token] = state

    t = threading.Thread(target=_qr_login_thread, args=(token,), daemon=True)
    t.start()

    # Wait up to 3 seconds for the URL to appear
    for _ in range(30):
        time.sleep(0.1)
        if state.get("url") or state["status"] in ("error", "expired"):
            break

    if not state.get("url"):
        with _qr_lock:
            _qr_sessions.pop(token, None)
        return {"success": False, "error": state.get("error", "Failed to generate QR code")}

    return {"success": True, "token": token, "url": state["url"]}


def check_qr_login(token: str) -> Dict[str, Any]:
    """Poll QR login status by token."""
    with _qr_lock:
        state = _qr_sessions.get(token)

    if not state:
        return {"success": False, "error": "QR session not found or expired"}

    status = state["status"]

    if status == "completed":
        with _qr_lock:
            _qr_sessions.pop(token, None)
        return {"success": True, "completed": True, "account_id": state.get("account_id")}

    if status in ("error", "expired"):
        with _qr_lock:
            _qr_sessions.pop(token, None)
        return {"success": False, "completed": False, "error": state.get("error")}

    # still waiting
    return {"success": True, "completed": False, "url": state.get("url"), "status": status}


# Global client manager instance
client_manager = ClientManager()
