"""
Telegram Client Manager - Multi-Account Orchestration
Handles concurrent user sessions using Telethon
"""
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, List, Callable, Any, Tuple

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


def _telegram_profile_mutation_restricted(err: str) -> bool:
    m = (err or "").lower()
    if "not available for frozen" in m:
        return True
    if "method that is not available" in m and "frozen" in m:
        return True
    return False


def _persist_profile_capability(account_id: int, status: str, reason: Optional[str] = None) -> None:
    try:
        with get_db_context() as db:
            acc = db.query(Account).filter(Account.id == account_id).first()
            if acc:
                acc.profile_capability_status = status
                acc.profile_capability_reason = (reason[:255] if reason else None)
                db.commit()
    except Exception:
        logger.warning("profile_capability_persist_failed", account_id=account_id, exc_info=True)


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

    def _resolve_session_file_path(self, account_id: int, account: Account) -> Optional[Path]:
        """Prefer canonical ``account_<id>.session``; then on-disk ``account.session_path``."""
        from src.core.session_paths import get_canonical_session_path

        canonical = get_canonical_session_path(account_id)
        if canonical.is_file():
            return canonical.resolve()
        sp = getattr(account, "session_path", None)
        if sp and str(sp).strip():
            alt = Path(str(sp).strip()).expanduser()
            if alt.is_file():
                return alt.resolve()
        return None

    async def _build_ephemeral_client_from_session_file(
        self, account_id: int
    ) -> Optional[TelegramClientWrapper]:
        """
        Build a Telethon client from an on-disk session file without registering in ``_clients``.
        Used by dashboard story-precheck in Gunicorn workers where ``initialize()`` was never run.
        """
        try:
            with get_db_context() as db:
                account = db.query(Account).filter(Account.id == account_id).first()
                if not account:
                    return None
                session_path = self._resolve_session_file_path(account_id, account)
                if not session_path:
                    return None
                db.expunge(account)
        except Exception as e:
            logger.warning("ephemeral_session_lookup_failed", account_id=account_id, error=str(e))
            return None

        try:
            client = TelegramClient(
                str(session_path),
                settings.telegram.api_id,
                settings.telegram.api_hash,
                proxy=account.proxy_config if account.proxy_config else None,
                device_model="STORYFLEET",
                app_version="1.0.0",
                system_version="Linux",
                lang_code="en",
            )
            return TelegramClientWrapper(account, client, self._rate_limiter)
        except Exception as e:
            logger.warning("ephemeral_client_construct_failed", account_id=account_id, error=str(e))
            return None

    async def get_fresh_client_for_story_publish(
        self, account_id: int
    ) -> Tuple[Optional[TelegramClientWrapper], Optional[str]]:
        """
        Connect client for one-off story/precheck flows. Returns (wrapper, error_message).

        Prefers an existing entry in ``_clients`` when present (scheduler/bot/celery).
        Otherwise loads a **one-off** client from the canonical session file or
        ``account.session_path`` on disk — without calling ``initialize()`` or mutating ``_clients``.

        Ephemeral wrappers set ``_precheck_disconnect_after = True`` so callers can disconnect
        without tearing down a shared pooled client (``_precheck_disconnect_after = False``).
        """
        wrapper = await self.get_client(account_id)
        ephemeral = False
        if not wrapper:
            wrapper = await self._build_ephemeral_client_from_session_file(account_id)
            ephemeral = bool(wrapper)
        if not wrapper:
            return None, "no_client"

        wrapper._precheck_disconnect_after = bool(ephemeral)

        if not wrapper.is_connected:
            ok = await wrapper.connect()
            if not ok:
                if ephemeral:
                    try:
                        await wrapper.disconnect()
                    except Exception:
                        pass
                return None, "connect_failed"
        if not await wrapper.client.is_user_authorized():
            if ephemeral:
                try:
                    await wrapper.disconnect()
                except Exception:
                    pass
            return None, "auth_required"
        return wrapper, None

    async def set_account_profile_photo(self, account_id: int, file_path: str) -> Dict[str, Any]:
        """Upload a profile photo; records profile_capability_* when Telegram blocks mutation APIs."""
        from telethon.tl import functions

        wrapper = await self.get_client(account_id)
        if not wrapper:
            return {"success": False, "error": "Account not available or not connected."}
        if not wrapper.is_connected:
            await wrapper.connect()
        if not wrapper.is_connected:
            return {"success": False, "error": "Account not connected."}
        try:
            uploaded = await wrapper.client.upload_file(file_path)
            await wrapper.client(functions.photos.UploadProfilePhotoRequest(file=uploaded))
            _persist_profile_capability(account_id, "allowed", None)
            return {"success": True}
        except Exception as e:
            err = str(e)
            if _telegram_profile_mutation_restricted(err):
                _persist_profile_capability(account_id, "restricted", err)
                return {
                    "success": False,
                    "error": err,
                    "profile_capability_status": "restricted",
                    "profile_capability_reason": err[:255],
                }
            return {"success": False, "error": err}

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


# Global client manager instance
client_manager = ClientManager()
