"""
Telegram Client Manager - Multi-Account Orchestration
Handles concurrent user sessions using Telethon
"""
import asyncio
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, List, Callable, Any, Union

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl import functions, types
from telethon.tl.types import Channel, Chat
from telethon.errors import (
    FloodWaitError,
    AuthKeyError,
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneNumberBannedError,
    AuthKeyUnregisteredError,
    UserDeactivatedBanError,
    UserDeactivatedError,
    SessionRevokedError,
)
from telethon.errors.rpcerrorlist import UserRestrictedError
from telethon.errors.rpcbaseerrors import RPCError
import structlog

from config.settings import settings
from src.core.models import Account, AccountStatus
from src.core.database import get_db_context
from .rate_limiter import RateLimiter

logger = structlog.get_logger(__name__)


def _mask_phone(phone: Optional[str], account_id: int) -> str:
    """Mask phone for logging; never log full number or session. E.g. +1513*****764"""
    if not phone or not isinstance(phone, str):
        return f"#{account_id}"
    phone = phone.strip()
    if phone.startswith("+"):
        digits = "".join(c for c in phone[1:] if c.isdigit())
    else:
        digits = "".join(c for c in phone if c.isdigit())
    if len(digits) < 4:
        return f"#{account_id}"
    return f"+{digits[:3]}*****{digits[-3:]}" if len(digits) >= 6 else f"#{account_id}"


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

    async def add_account(self, account: Union[Account, int]) -> Optional[TelegramClientWrapper]:
        """Add a new account to the manager. Accepts Account instance or account id (int)."""
        async with self._lock:
            if isinstance(account, int):
                with get_db_context() as db:
                    account = db.query(Account).filter(Account.id == account).first()
                    if not account or not account.session_string:
                        return None
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
        """Get a client by account ID. Adds and connects the account if not already in the manager."""
        wrapper = self._clients.get(account_id)
        if wrapper is not None and wrapper.is_connected:
            return wrapper
        with get_db_context() as db:
            account = db.query(Account).filter(Account.id == account_id).first()
            if not account or not account.session_string:
                logger.warning("Account not found or no session", account_id=account_id)
                return None
        if wrapper is None:
            wrapper = await self.add_account(account_id)
        if wrapper is None:
            return None
        if not wrapper.is_connected:
            ok = await wrapper.connect()
            if not ok:
                logger.warning("Client failed to connect", account_id=account_id)
                return None
        return wrapper

    async def get_dialogs(self, account_id: int, limit: int = 200) -> List[Dict[str, Any]]:
        """Fetch groups/channels the account is in. Returns list of {id, title, username, chat_type}."""
        with get_db_context() as db:
            account = db.query(Account).filter(Account.id == account_id).first()
            if not account or not account.session_string:
                return []
        wrapper = await self.get_client(account_id)
        if not wrapper:
            await self.add_account(account)
            wrapper = await self.get_client(account_id)
        if not wrapper:
            return []
        if not wrapper.is_connected:
            await wrapper.connect()
        if not wrapper.is_connected:
            return []
        try:
            dialogs = await wrapper.client.get_dialogs(limit=limit)
            result = []
            for d in dialogs:
                e = d.entity
                if isinstance(e, Chat):
                    result.append({
                        "id": e.id, "title": getattr(e, "title", None) or str(e.id),
                        "username": getattr(e, "username", None), "chat_type": "group",
                    })
                elif isinstance(e, Channel):
                    title = getattr(e, "title", None) or str(e.id)
                    username = getattr(e, "username", None)
                    ct = "channel" if getattr(e, "broadcast", False) else "supergroup"
                    result.append({
                        "id": e.id, "title": title, "username": username,
                        "chat_type": ct,
                    })
            return result
        except Exception as e:
            logger.error("get_dialogs failed", account_id=account_id, error=str(e))
            return []

    async def set_account_username(self, account_id: int, username: str) -> Dict[str, Any]:
        """Set Telegram username for an account. Username without @."""
        username = (username or "").strip().replace("@", "").strip()
        if not username or len(username) < 5:
            return {"success": False, "error": "Username must be 5–32 characters (without @)."}
        wrapper = await self.get_client(account_id)
        if not wrapper:
            return {"success": False, "error": "Account not available or not connected."}
        try:
            await wrapper.client(functions.account.UpdateUsernameRequest(username=username))
            with get_db_context() as db:
                acc = db.query(Account).filter(Account.id == account_id).first()
                if acc:
                    acc.username = username
                    db.commit()
            return {"success": True, "username": username}
        except Exception as e:
            err = str(e)
            if "USERNAME_INVALID" in err or "Invalid" in err:
                return {"success": False, "error": "Username invalid. Use 5–32 characters, letters, numbers, underscores."}
            if "USERNAME_OCCUPIED" in err or "occupied" in err.lower():
                return {"success": False, "error": "This username is already taken."}
            return {"success": False, "error": err}

    async def set_account_profile_photo(self, account_id: int, file_path: str) -> Dict[str, Any]:
        """Set profile photo for an account from a local file path."""
        wrapper = await self.get_client(account_id)
        if not wrapper:
            return {"success": False, "error": "Account not available or not connected."}
        try:
            uploaded = await wrapper.client.upload_file(file_path)
            await wrapper.client(functions.photos.UploadProfilePhotoRequest(file=uploaded))
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

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

    async def check_accounts_health(
        self,
        update_status: bool = False,
        account_ids: Optional[List[int]] = None,
        verbose: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Check accounts: connect + multiple API calls to detect deleted/banned/auth.
        Returns list of {account_id, phone, status, message, reason_code, checked_at}.

        Classification rules:
          - auth_required: no session | not authorized | SessionRevokedError | AuthKeyUnregisteredError | AuthKeyError
          - deleted: User.deleted=True | UserDeactivatedError | UserDeactivatedBanError | RPCError 401 / deactivated
          - banned: UserDeactivatedBanError
          - restricted: User.restricted=True | UserRestrictedError | RPCError restricted
          - flood_wait: FloodWaitError
          - alive: only if ALL of (get_dialogs(1), get_me(), GetFullUser, GetAccountTTL) succeed and no bad flags
        """
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        results: List[Dict[str, Any]] = []
        with get_db_context() as db:
            q = db.query(Account).filter(Account.session_string.isnot(None))
            if account_ids is not None:
                q = q.filter(Account.id.in_(account_ids))
            accounts = q.all()
            account_list = [(a.id, a.phone_number, a.session_string, a.status, getattr(a, "username", None)) for a in accounts]

        for account_id, phone, session_string, _status, username in account_list:
            masked = _mask_phone(phone, account_id)
            checked_at = datetime.now(timezone.utc).isoformat()

            if verbose:
                logger.info(
                    "alive_check_start",
                    account_id=account_id,
                    phone_masked=masked,
                    username=username,
                    checked_at=checked_at,
                )

            if not session_string or not session_string.strip():
                results.append({
                    "account_id": account_id,
                    "phone": phone or f"#{account_id}",
                    "status": "error",
                    "message": "No session",
                    "reason_code": "no_session",
                    "checked_at": checked_at,
                })
                if update_status:
                    with get_db_context() as db:
                        acc = db.query(Account).filter(Account.id == account_id).first()
                        if acc:
                            acc.status = AccountStatus.AUTH_REQUIRED
                continue

            client = TelegramClient(
                StringSession(session_string.strip()),
                settings.telegram.api_id,
                settings.telegram.api_hash,
            )
            try:
                # Step 1: connect
                await client.connect()
                if verbose:
                    logger.info("alive_check_step", account_id=account_id, step="connect", result="ok")

                # Step 2: is_user_authorized
                if not await client.is_user_authorized():
                    if verbose:
                        logger.info("alive_check_step", account_id=account_id, step="is_user_authorized", result="not_authorized")
                    results.append({
                        "account_id": account_id,
                        "phone": phone or f"#{account_id}",
                        "status": "auth_required",
                        "message": "Session not authorized",
                        "reason_code": "not_authorized",
                        "checked_at": checked_at,
                    })
                    if update_status:
                        with get_db_context() as db:
                            acc = db.query(Account).filter(Account.id == account_id).first()
                            if acc:
                                acc.status = AccountStatus.AUTH_REQUIRED
                    await client.disconnect()
                    continue

                if verbose:
                    logger.info("alive_check_step", account_id=account_id, step="is_user_authorized", result="ok")

                # Step 3: get_dialogs(limit=1) FIRST — triggers USER_DEACTIVATED for deleted accounts
                await client.get_dialogs(limit=1)
                if verbose:
                    logger.info("alive_check_step", account_id=account_id, step="get_dialogs(limit=1)", result="ok")

                # Step 4: get_me()
                me = await client.get_me()
                if verbose:
                    user_flags = {
                        "deleted": getattr(me, "deleted", None),
                        "restricted": getattr(me, "restricted", None),
                        "bot": getattr(me, "bot", None),
                        "scam": getattr(me, "scam", None),
                        "fake": getattr(me, "fake", None),
                    }
                    logger.info(
                        "alive_check_step",
                        account_id=account_id,
                        step="get_me",
                        result="ok",
                        user_id=me.id,
                        user_flags=user_flags,
                    )

                if getattr(me, "deleted", False):
                    await client.disconnect()
                    results.append({
                        "account_id": account_id,
                        "phone": phone or f"#{account_id}",
                        "status": "deleted",
                        "message": "Account deleted (User.deleted=True)",
                        "reason_code": "user_deleted_flag",
                        "checked_at": checked_at,
                    })
                    if update_status:
                        with get_db_context() as db:
                            acc = db.query(Account).filter(Account.id == account_id).first()
                            if acc:
                                acc.status = AccountStatus.AUTH_REQUIRED
                    await asyncio.sleep(0.5)
                    continue
                if getattr(me, "restricted", False):
                    await client.disconnect()
                    results.append({
                        "account_id": account_id,
                        "phone": phone or f"#{account_id}",
                        "status": "restricted",
                        "message": "Account restricted (User.restricted=True)",
                        "reason_code": "user_restricted_flag",
                        "checked_at": checked_at,
                    })
                    if update_status:
                        with get_db_context() as db:
                            acc = db.query(Account).filter(Account.id == account_id).first()
                            if acc:
                                acc.status = AccountStatus.AUTH_REQUIRED
                    await asyncio.sleep(0.5)
                    continue

                # Step 5: GetFullUser (server-side validation)
                await client(functions.users.GetFullUserRequest(me))
                if verbose:
                    logger.info("alive_check_step", account_id=account_id, step="GetFullUserRequest", result="ok")

                # Step 6: GetAccountTTL (fails for many deleted/limited)
                await client(functions.account.GetAccountTTLRequest())
                if verbose:
                    logger.info("alive_check_step", account_id=account_id, step="GetAccountTTLRequest", result="ok")

                # Step 7: updates.GetState (lightweight server validation)
                await client(functions.updates.GetStateRequest())
                if verbose:
                    logger.info("alive_check_step", account_id=account_id, step="GetStateRequest", result="ok")

                await client.disconnect()

                # Only mark ALIVE when all checks passed
                results.append({
                    "account_id": account_id,
                    "phone": phone or f"#{account_id}",
                    "status": "alive",
                    "message": (f"@{me.username}" if me.username else (me.first_name or str(me.id))),
                    "reason_code": "all_checks_passed",
                    "checked_at": checked_at,
                })
                if update_status:
                    with get_db_context() as db:
                        acc = db.query(Account).filter(Account.id == account_id).first()
                        if acc and acc.status != AccountStatus.ACTIVE:
                            acc.status = AccountStatus.ACTIVE

            except FloodWaitError as e:
                try:
                    await client.disconnect()
                except Exception:
                    pass
                if verbose:
                    logger.info(
                        "alive_check_exception",
                        account_id=account_id,
                        exception_type=type(e).__name__,
                        message=str(e),
                        seconds=getattr(e, "seconds", None),
                    )
                results.append({
                    "account_id": account_id,
                    "phone": phone or f"#{account_id}",
                    "status": "flood_wait",
                    "message": f"Rate limited; wait {getattr(e, 'seconds', 0)}s",
                    "reason_code": "flood_wait",
                    "checked_at": checked_at,
                })
                if update_status:
                    with get_db_context() as db:
                        acc = db.query(Account).filter(Account.id == account_id).first()
                        if acc:
                            acc.status = AccountStatus.FLOOD_WAIT
            except UserDeactivatedBanError as e:
                await client.disconnect()
                if verbose:
                    logger.info("alive_check_exception", account_id=account_id, exception_type=type(e).__name__, message=str(e))
                results.append({
                    "account_id": account_id,
                    "phone": phone or f"#{account_id}",
                    "status": "banned",
                    "message": "Account banned by Telegram",
                    "reason_code": "UserDeactivatedBanError",
                    "checked_at": checked_at,
                })
                if update_status:
                    with get_db_context() as db:
                        acc = db.query(Account).filter(Account.id == account_id).first()
                        if acc:
                            acc.status = AccountStatus.BANNED
            except UserDeactivatedError as e:
                await client.disconnect()
                if verbose:
                    logger.info("alive_check_exception", account_id=account_id, exception_type=type(e).__name__, message=str(e))
                results.append({
                    "account_id": account_id,
                    "phone": phone or f"#{account_id}",
                    "status": "deleted",
                    "message": "Account deleted or deactivated",
                    "reason_code": "UserDeactivatedError",
                    "checked_at": checked_at,
                })
                if update_status:
                    with get_db_context() as db:
                        acc = db.query(Account).filter(Account.id == account_id).first()
                        if acc:
                            acc.status = AccountStatus.AUTH_REQUIRED
            except UserRestrictedError as e:
                try:
                    await client.disconnect()
                except Exception:
                    pass
                if verbose:
                    logger.info("alive_check_exception", account_id=account_id, exception_type=type(e).__name__, message=str(e))
                results.append({
                    "account_id": account_id,
                    "phone": phone or f"#{account_id}",
                    "status": "restricted",
                    "message": "Account restricted by Telegram",
                    "reason_code": "UserRestrictedError",
                    "checked_at": checked_at,
                })
                if update_status:
                    with get_db_context() as db:
                        acc = db.query(Account).filter(Account.id == account_id).first()
                        if acc:
                            acc.status = AccountStatus.AUTH_REQUIRED
            except (SessionRevokedError, AuthKeyUnregisteredError, AuthKeyError) as e:
                try:
                    await client.disconnect()
                except Exception:
                    pass
                if verbose:
                    logger.info("alive_check_exception", account_id=account_id, exception_type=type(e).__name__, message=str(e))
                results.append({
                    "account_id": account_id,
                    "phone": phone or f"#{account_id}",
                    "status": "auth_required",
                    "message": "Session invalid or revoked",
                    "reason_code": type(e).__name__,
                    "checked_at": checked_at,
                })
                if update_status:
                    with get_db_context() as db:
                        acc = db.query(Account).filter(Account.id == account_id).first()
                        if acc:
                            acc.status = AccountStatus.AUTH_REQUIRED
            except RPCError as e:
                try:
                    await client.disconnect()
                except Exception:
                    pass
                msg = str(e).lower()
                code = getattr(e, "code", None)
                if code == 401 or "deactivated" in msg or "user_deactivated" in msg:
                    status, message, reason_code = "deleted", "Account deleted or deactivated (RPC)", f"RPCError_code_{code}"
                elif code == 420 or "frozen" in msg:
                    status, message, reason_code = "frozen", "Account frozen by Telegram (limited methods)", f"RPCError_code_{code}"
                elif "restricted" in msg:
                    status, message, reason_code = "restricted", str(e), "RPCError_restricted"
                else:
                    status, message, reason_code = "error", str(e), f"RPCError_{code}"
                if verbose:
                    logger.info("alive_check_exception", account_id=account_id, exception_type=type(e).__name__, code=code, message=str(e))
                results.append({
                    "account_id": account_id,
                    "phone": phone or f"#{account_id}",
                    "status": status,
                    "message": message,
                    "reason_code": reason_code,
                    "checked_at": checked_at,
                })
                if update_status and status in ("deleted", "restricted", "frozen"):
                    with get_db_context() as db:
                        acc = db.query(Account).filter(Account.id == account_id).first()
                        if acc:
                            acc.status = AccountStatus.BANNED if status == "restricted" else AccountStatus.AUTH_REQUIRED
            except Exception as e:
                try:
                    await client.disconnect()
                except Exception:
                    pass
                if verbose:
                    logger.info("alive_check_exception", account_id=account_id, exception_type=type(e).__name__, message=str(e))
                results.append({
                    "account_id": account_id,
                    "phone": phone or f"#{account_id}",
                    "status": "error",
                    "message": str(e),
                    "reason_code": type(e).__name__,
                    "checked_at": checked_at,
                })

            await asyncio.sleep(0.5)

        return results

    async def start_phone_auth(self, phone_number: str) -> Dict[str, Any]:
        """Start phone authentication for a new account"""
        # Normalize phone for comparison
        phone_norm = phone_number.strip().replace(" ", "").replace("-", "")
        if not phone_norm.startswith("+"):
            phone_norm = "+" + phone_norm

        # If this number is already in our accounts, the code goes to that session (not SMS)
        with get_db_context() as db:
            for acc in db.query(Account).all():
                p = (acc.phone_number or "").replace(" ", "").replace("-", "").strip()
                if not p.startswith("+"):
                    p = "+" + p
                if p == phone_norm or (len(p) >= 9 and len(phone_norm) >= 9 and p[-9:] == phone_norm[-9:]):
                    return {
                        "success": False,
                        "error": f"This number is already added (Account #{acc.id}). Use the phone icon next to it to get the code.",
                        "existing_account_id": acc.id,
                    }

        # Create temporary client for auth
        session = StringSession()
        client = TelegramClient(
            session,
            settings.telegram.api_id,
            settings.telegram.api_hash,
        )

        try:
            await client.connect()
            # Use raw API with CodeSettings to prefer app delivery (Telegram "Login code: XXXXX")
            # allow_app_hash=True, allow_missed_call=True = we support app/missed-call (not SMS-only)
            sent_code = await client(
                functions.auth.SendCodeRequest(
                    phone_number=phone_number,
                    api_id=settings.telegram.api_id,
                    api_hash=settings.telegram.api_hash,
                    settings=types.CodeSettings(
                        allow_app_hash=True,
                        allow_missed_call=True,
                    ),
                )
            )

            # Handle SentCodeSuccess (already logged in - shouldn't happen for new login)
            if isinstance(sent_code, types.auth.SentCodeSuccess):
                return {"success": False, "error": "This number is already logged in elsewhere."}

            # Keep connection briefly (Telegram may not deliver if we disconnect immediately)
            await asyncio.sleep(3)

            # Check if this number exists in our accounts – code may arrive there, not SMS
            existing_id = None
            with get_db_context() as db:
                for acc in db.query(Account).all():
                    p = (acc.phone_number or "").replace(" ", "").replace("-", "").strip()
                    if not p.startswith("+"):
                        p = "+" + p
                    if p == phone_norm or (len(p) >= 9 and len(phone_norm) >= 9 and p[-9:] == phone_norm[-9:]):
                        existing_id = acc.id
                        break

            return {
                "success": True,
                "phone_number": phone_number,
                "phone_code_hash": sent_code.phone_code_hash,
                "session_string": session.save(),
                "message": f"Code sent to {phone_number}",
                "existing_account_id": existing_id,
            }
        except PhoneNumberBannedError:
            return {"success": False, "error": "Phone number is banned"}
        except FloodWaitError as e:
            hours = round(e.seconds / 3600, 1)
            return {
                "success": False,
                "error": f"Telegram rate limit: too many code requests. Wait ~{hours} hours before trying again, or use a different phone number."
            }
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
        """Import account from session string (e.g. from tdata conversion). No phone/code needed."""
        if not session_string or not session_string.strip():
            return {"success": False, "error": "Session string required"}
        session = StringSession(session_string.strip())
        client = TelegramClient(
            session,
            settings.telegram.api_id,
            settings.telegram.api_hash,
            device_model="STORYFLEET",
            app_version="1.0.0",
            system_version="Linux",
            lang_code="en",
        )
        try:
            await client.connect()
            if not await client.is_user_authorized():
                return {"success": False, "error": "Session not authorized. Use a valid session from tdata or Telegram."}
            me = await client.get_me()
            # Phone: from get_me() or GetFullUser (some sessions don't expose phone in get_me)
            phone_number = f"+{me.phone}" if getattr(me, "phone", None) else None
            if not phone_number:
                try:
                    full = await client(functions.users.GetFullUserRequest(me))
                    if getattr(full, "full_user", None) and getattr(full.full_user, "phone", None):
                        p = full.full_user.phone
                        phone_number = (p if (p and str(p).startswith("+")) else f"+{p}") if p else None
                except Exception:
                    pass
                if not phone_number:
                    phone_number = f"user_{me.id}"
            with get_db_context() as db:
                existing = db.query(Account).filter(Account.user_id == me.id).first()
                if existing:
                    existing.session_string = session.save()
                    existing.last_active = datetime.utcnow()
                    # Refresh profile from Telegram so username/first_name/last_name/phone are up to date
                    existing.phone_number = phone_number
                    existing.username = getattr(me, "username", None)
                    existing.first_name = getattr(me, "first_name", None)
                    existing.last_name = getattr(me, "last_name", None)
                    db.commit()
                    db.refresh(existing)
                    await self.add_account(existing)
                    return {
                        "success": True,
                        "account_id": existing.id,
                        "user_id": me.id,
                        "username": me.username,
                        "phone_number": getattr(existing, "phone_number", None) or phone_number,
                        "first_name": getattr(me, "first_name", None),
                        "last_name": getattr(me, "last_name", None),
                        "message": f"Session updated for {me.first_name}",
                    }
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
                await self.add_account(account)
                return {
                    "success": True,
                    "account_id": account.id,
                    "user_id": me.id,
                    "username": me.username,
                    "phone_number": phone_number,
                    "first_name": me.first_name,
                    "last_name": me.last_name,
                    "message": f"Successfully imported {me.first_name}",
                }
        except AuthKeyError:
            return {"success": False, "error": "Invalid session. Convert tdata again or use a fresh session."}
        except Exception as e:
            return {"success": False, "error": str(e)}
        finally:
            await client.disconnect()


# Pending QR logins: token -> {url, status, account_id?, error?}
_pending_qr: Dict[str, Dict[str, Any]] = {}
_qr_lock = threading.Lock()


def _qr_login_thread(token: str) -> None:
    """Background thread: create client, qr_login, wait for scan, save account."""
    async def _run():
        client = None
        try:
            session = StringSession()
            client = TelegramClient(
                session,
                settings.telegram.api_id,
                settings.telegram.api_hash,
            )
            await client.connect()
            qr = await client.qr_login()
            with _qr_lock:
                _pending_qr[token]["url"] = qr.url
                _pending_qr[token]["status"] = "waiting"
            await qr.wait(timeout=120)
            me = await client.get_me()
            phone = f"+{me.phone}" if me.phone else f"user_{me.id}"
            with get_db_context() as db:
                acc = Account(
                    phone_number=phone,
                    session_string=session.save(),
                    user_id=me.id,
                    username=me.username,
                    first_name=me.first_name,
                    last_name=me.last_name,
                    status=AccountStatus.ACTIVE,
                    last_active=datetime.utcnow(),
                )
                db.add(acc)
                db.commit()
                db.refresh(acc)
            await client_manager.add_account(acc)
            with _qr_lock:
                _pending_qr[token]["status"] = "success"
                _pending_qr[token]["account_id"] = acc.id
        except asyncio.TimeoutError:
            with _qr_lock:
                _pending_qr[token]["status"] = "expired"
                _pending_qr[token]["error"] = "QR code expired. Generate a new one."
        except SessionPasswordNeededError:
            with _qr_lock:
                _pending_qr[token]["status"] = "error"
                _pending_qr[token]["error"] = "2FA password required. Use Import from tdata or Phone+Code instead."
        except Exception as e:
            with _qr_lock:
                _pending_qr[token]["status"] = "error"
                _pending_qr[token]["error"] = str(e)
        finally:
            if client:
                await client.disconnect()

    asyncio.run(_run())


def start_qr_login() -> Dict[str, Any]:
    """Start QR login. Returns token and URL. Background thread waits for scan."""
    api_id = getattr(settings.telegram, "api_id", None) or int(__import__("os").environ.get("TELEGRAM_API_ID", 0) or 0)
    api_hash = getattr(settings.telegram, "api_hash", None) or __import__("os").environ.get("TELEGRAM_API_HASH", "")
    if not api_id or not api_hash:
        return {"success": False, "error": "TELEGRAM_API_ID and TELEGRAM_API_HASH required. Run sync_db_from_server with --env."}
    token = str(uuid.uuid4())
    with _qr_lock:
        _pending_qr[token] = {"url": None, "status": "starting"}
    t = threading.Thread(target=_qr_login_thread, args=(token,))
    t.daemon = True
    t.start()
    for _ in range(50):
        time.sleep(0.2)
        with _qr_lock:
            if _pending_qr[token].get("url"):
                return {"success": True, "token": token, "url": _pending_qr[token]["url"]}
            if _pending_qr[token].get("status") == "error":
                return {"success": False, "error": _pending_qr[token].get("error", "Unknown error")}
    return {"success": False, "error": "QR login failed to start"}


def check_qr_login(token: str) -> Dict[str, Any]:
    """Check if QR login completed."""
    with _qr_lock:
        if token not in _pending_qr:
            return {"success": False, "error": "Invalid or expired token"}
        p = _pending_qr[token]
        if p["status"] == "success":
            return {"success": True, "account_id": p.get("account_id")}
        if p["status"] in ("error", "expired"):
            return {"success": False, "error": p.get("error", "Login failed")}
        return {"success": False, "status": "waiting"}


# Global client manager instance
client_manager = ClientManager()
