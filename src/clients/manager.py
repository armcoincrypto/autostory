"""
Telegram Client Manager - Multi-Account Orchestration
Handles concurrent user sessions using Telethon
"""
import asyncio
import secrets
import sqlite3
import threading
import time
from datetime import datetime
import os
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
from telethon.tl.types import User, Chat, Channel
from telethon.utils import get_peer_id
import structlog

import sys
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
from config.settings import settings
from src.core.models import Account, AccountStatus
from src.core.database import get_db_context
from .rate_limiter import RateLimiter
from .session_resolve import (
    resolve_telethon_session,
    probe_telethon_session_kind,
    human_message_for_code,
    ERR_SESSION_FILE_MISSING,
    ERR_INVALID_SESSION_FORMAT,
    ERR_EMPTY_SESSION,
)
from src.core.session_lock import acquire_session_lock, SessionLockHandle

logger = structlog.get_logger(__name__)

# Telethon connect retries (transient network / SQLite session contention).
_CONNECT_ATTEMPTS = 4
_CONNECT_BACKOFF_BASE_S = 0.5
_CONNECT_BACKOFF_CAP_S = 30.0


def _connect_failure_code(exc: Optional[BaseException]) -> str:
    """
    Classify Telethon connect failures for readiness / pool (not Telegram auth).
    Auth failures are handled separately via ``is_user_authorized`` false.
    """
    if exc is None:
        return "failed_connect"
    if isinstance(exc, sqlite3.OperationalError):
        if "locked" in str(exc).lower():
            return "session_db_locked"
        return "failed_connect"
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "failed_connect_network"
    if isinstance(exc, OSError):
        # Broken pipe, reset, network unreachable, etc.
        return "failed_connect_network"
    msg = str(exc).lower()
    if "database is locked" in msg or "database locked" in msg:
        return "session_db_locked"
    if "server closed" in msg or "connection reset" in msg or "timed out" in msg:
        return "failed_connect_network"
    return "failed_connect"


class TelegramClientWrapper:
    """Wrapper around Telethon client with additional functionality"""

    def __init__(
        self,
        account: Account,
        client: TelegramClient,
        rate_limiter: RateLimiter,
        session_file_lock: Optional[SessionLockHandle] = None,
    ):
        self.account = account
        self.client = client
        self.rate_limiter = rate_limiter
        self.is_connected = False
        self._lock = asyncio.Lock()
        # Thread that completed a successful authorized connect (no cross-thread reuse).
        self._owner_thread_id: Optional[int] = None
        # Exclusive fcntl lock while SQLite session file is open (cross-process safety).
        self._session_file_lock: Optional[SessionLockHandle] = session_file_lock

    async def connect_with_reason(self) -> Tuple[bool, Optional[str]]:
        """
        Connect and verify user authorization.
        Returns (ok, failure_code) where failure_code is None on success.
        """
        async with self._lock:
            last_err: Optional[BaseException] = None
            for attempt in range(1, _CONNECT_ATTEMPTS + 1):
                try:
                    await self.client.connect()
                    if await self.client.is_user_authorized():
                        self.is_connected = True
                        self._owner_thread_id = threading.get_ident()
                        me = await self.client.get_me()
                        logger.debug(
                            "Client connected",
                            account_id=self.account.id,
                            phone=self.account.phone_number,
                            user_id=me.id,
                            username=me.username,
                            connect_attempt=attempt,
                        )
                        return True, None
                    logger.warning(
                        "Client not authorized",
                        account_id=self.account.id,
                        phone=self.account.phone_number,
                    )
                    return False, "unauthorized_session"
                except Exception as e:
                    last_err = e
                    logger.warning(
                        "Connection attempt failed",
                        account_id=self.account.id,
                        phone=self.account.phone_number,
                        error=str(e),
                        connect_attempt=attempt,
                        max_attempts=_CONNECT_ATTEMPTS,
                    )
                    try:
                        if self.client.is_connected():
                            await self.client.disconnect()
                    except Exception:
                        pass
                    self.is_connected = False
                    if attempt >= _CONNECT_ATTEMPTS:
                        logger.error(
                            "Connection failed after retries",
                            account_id=self.account.id,
                            phone=self.account.phone_number,
                            error=str(last_err),
                        )
                        return False, _connect_failure_code(last_err)
                    delay = min(
                        _CONNECT_BACKOFF_CAP_S,
                        _CONNECT_BACKOFF_BASE_S * (2 ** (attempt - 1)),
                    )
                    await asyncio.sleep(delay)
            return False, _connect_failure_code(last_err)

    async def connect(self) -> bool:
        """Connect to Telegram"""
        ok, _ = await self.connect_with_reason()
        return ok

    async def disconnect(self) -> None:
        """Disconnect from Telegram"""
        async with self._lock:
            if self.client.is_connected():
                await self.client.disconnect()
                self.is_connected = False
                self._owner_thread_id = None
                logger.debug("Client disconnected", phone=self.account.phone_number)
            if self._session_file_lock is not None:
                try:
                    self._session_file_lock.release()
                except Exception:
                    pass
                self._session_file_lock = None

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
        # Serialize connect/create per account on the *current* event loop only.
        self._account_connect_locks: Dict[int, asyncio.Lock] = {}
        self._account_connect_lock_loop: Dict[int, asyncio.AbstractEventLoop] = {}
        self._session_dir = Path(settings.storage.sessions_dir)
        self._session_dir.mkdir(parents=True, exist_ok=True)

    def _telethon_loop_mismatch(self, wrapper: TelegramClientWrapper) -> bool:
        """
        Telethon binds each TelegramClient to the asyncio loop that first called
        ``connect``. Flask uses ``asyncio.new_event_loop()`` per request (see
        ``run_async`` / ``asyncio.run``); reusing a cached client raises:
        "The asyncio event loop must not change after connection".
        """
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            return False
        bound = getattr(wrapper.client, "_loop", None)
        return bound is not None and bound is not current

    def _telethon_thread_mismatch(self, wrapper: TelegramClientWrapper) -> bool:
        """True if the client was connected on another OS thread (Telethon is not thread-safe)."""
        tid = wrapper._owner_thread_id
        return tid is not None and tid != threading.get_ident()

    def _stale_client_reason(self, wrapper: TelegramClientWrapper) -> Optional[str]:
        if self._telethon_loop_mismatch(wrapper):
            return "event_loop_mismatch"
        if self._telethon_thread_mismatch(wrapper):
            return "thread_mismatch"
        return None

    def _ensure_account_connect_lock_unlocked(self, account_id: int) -> asyncio.Lock:
        """Bind per-account connect lock to the running event loop. Caller holds ``self._lock``."""
        loop = asyncio.get_running_loop()
        prev = self._account_connect_lock_loop.get(account_id)
        if prev is not loop:
            if prev is not None:
                logger.info(
                    "telethon_client_manager_account_lock_rebound",
                    account_id=account_id,
                    reason="event_loop_changed_for_account_connect_lock",
                )
            self._account_connect_locks[account_id] = asyncio.Lock()
            self._account_connect_lock_loop[account_id] = loop
        return self._account_connect_locks[account_id]

    async def _drop_stale_client_unlocked(self, account_id: int) -> bool:
        """
        Remove pool entry if the Telethon client is tied to another loop or thread.
        Caller must hold ``self._lock``. Awaits ``disconnect`` when possible.
        """
        w = self._clients.get(account_id)
        if not w:
            return False
        reason = self._stale_client_reason(w)
        if not reason:
            return False
        if reason == "event_loop_mismatch":
            logger.warning(
                "telethon_event_loop_mismatch",
                account_id=account_id,
                message="cached client bound to a different asyncio loop; will recreate",
            )
        else:
            logger.warning(
                "telethon_thread_mismatch",
                account_id=account_id,
                message="cached client was connected on a different thread; will recreate",
            )
        logger.info(
            "telethon_client_recycled",
            account_id=account_id,
            reason=reason,
        )
        await self._remove_client_unlocked(account_id)
        return True

    async def _remove_client_unlocked(self, account_id: int) -> None:
        """Pop a client from the pool and disconnect (caller must hold self._lock)."""
        if account_id not in self._clients:
            return
        w = self._clients.pop(account_id)
        try:
            await w.disconnect()
        except Exception:
            pass

    async def initialize(self) -> None:
        """
        Legacy entrypoint for workers/Celery.

        **Does not** preload Telegram sessions — connecting every account at process
        start fights the web tier for SQLite ``.session`` files. Use lazy
        ``add_account`` from the executor / API handlers instead.
        """
        logger.info(
            "ClientManager initialize() — skipping bulk Telegram preload (lazy connect per job)",
            pooled_clients=len(self._clients),
        )

    async def add_account(
        self,
        account: Account,
        *,
        session_lock_timeout_sec: float = 60.0,
    ) -> Tuple[Optional[TelegramClientWrapper], Optional[str]]:
        """
        Add an account: resolve session (file vs string), connect, require authorization.

        Returns (wrapper, None) on success, or (None, failure_code) on failure.
        """
        async with self._lock:
            ac_lock = self._ensure_account_connect_lock_unlocked(account.id)

        async with ac_lock:
            async with self._lock:
                await self._drop_stale_client_unlocked(account.id)
                if account.id in self._clients:
                    existing = self._clients[account.id]
                    ok, reason = await existing.connect_with_reason()
                    if ok:
                        return existing, None
                    logger.warning(
                        "Existing client not usable; rebuilding",
                        account_id=account.id,
                        reason=reason,
                    )
                    await self._remove_client_unlocked(account.id)

            session, session_kind, resolve_err = resolve_telethon_session(account)
            if resolve_err:
                logger.error(
                    "Failed to add account",
                    account_id=account.id,
                    session_kind=session_kind,
                    reason=resolve_err,
                    error=human_message_for_code(resolve_err),
                )
                return None, resolve_err

            session_file_lock: Optional[SessionLockHandle] = None
            if session_kind == "file":
                ok_lock, lock_handle, lock_err = acquire_session_lock(
                    account.id, timeout_sec=float(session_lock_timeout_sec),
                )
                if not ok_lock:
                    logger.warning(
                        "session_file_lock_not_acquired",
                        account_id=account.id,
                        detail=lock_err,
                    )
                    return None, "session_lock_timeout"
                session_file_lock = lock_handle

            try:
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
                wrapper = TelegramClientWrapper(
                    account, client, self._rate_limiter, session_file_lock=session_file_lock,
                )
                session_file_lock = None  # wrapper owns release on disconnect
            except Exception as e:
                if session_file_lock is not None:
                    try:
                        session_file_lock.release()
                    except Exception:
                        pass
                logger.error(
                    "Failed to add account",
                    account_id=account.id,
                    session_kind=session_kind,
                    error=str(e),
                )
                return None, "failed_connect"

            async def _release_orphan_wrapper(reason: str) -> None:
                """Disconnect wrapper not yet registered in ``_clients`` (releases session flock)."""
                try:
                    await wrapper.disconnect()
                except Exception:
                    pass
                logger.info(
                    "client_manager_orphan_wrapper_released",
                    account_id=account.id,
                    reason=reason,
                )

            try:
                ok, connect_reason = await wrapper.connect_with_reason()
            except asyncio.CancelledError:
                await _release_orphan_wrapper("cancelled")
                raise
            except Exception:
                await _release_orphan_wrapper("connect_failed")
                raise

            if not ok:
                await _release_orphan_wrapper("connect_failed")
                logger.warning(
                    "Failed to add account — not authorized or connect failed",
                    account_id=account.id,
                    session_kind=session_kind,
                    reason=connect_reason,
                    error=human_message_for_code(connect_reason),
                )
                return None, connect_reason

            async with self._lock:
                self._clients[account.id] = wrapper
            logger.debug(
                "Account added",
                account_id=account.id,
                phone=account.phone_number,
                session_kind=session_kind,
            )
            return wrapper, None

    async def connect_account(
        self,
        account_id: int,
        *,
        session_lock_timeout_sec: Optional[float] = None,
    ) -> Tuple[Optional[TelegramClientWrapper], Optional[str]]:
        """Load account from DB and register a verified client (same as add_account)."""
        with get_db_context() as db:
            account = db.query(Account).filter(Account.id == account_id).first()
        if not account:
            return None, "account_not_found"
        lock_to = 60.0 if session_lock_timeout_sec is None else float(session_lock_timeout_sec)
        return await self.add_account(account, session_lock_timeout_sec=lock_to)

    async def collect_accounts_readiness(
        self, deep: bool = False, only_account_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Read-only readiness snapshot for operators.
        When deep=False: filesystem + StringSession parse only (no Telegram network;
        no ``SQLiteSession`` / session DB open — see ``probe_telethon_session_kind``).
        ``ready`` is **never** set True in shallow mode — a session file existing is not
        proof of Telegram authorization (see ``_readiness_row``).
        When deep=True: connect + is_user_authorized per account (slower; hits Telegram).
        """
        from src.clients import readiness_store
        from src.core.scheduler_models import AccountReadinessSnapshot

        with get_db_context() as db:
            q = db.query(Account).order_by(Account.id)
            if only_account_id is not None:
                q = q.filter(Account.id == int(only_account_id))
            accounts = q.all()
            now = readiness_store._now_naive()
            aid_list = [int(a.id) for a in accounts]
            snaps = {}
            if aid_list:
                for s in db.query(AccountReadinessSnapshot).filter(
                    AccountReadinessSnapshot.account_id.in_(aid_list)
                ).all():
                    snaps[int(s.account_id)] = s

        rows: List[Dict[str, Any]] = []
        for account in accounts:
            aid = int(account.id)
            snap = snaps.get(aid)

            # Always start with shallow + cached overlay (fast, stable).
            base = await self._readiness_row(account, deep=False)
            if snap and readiness_store.snapshot_row_valid(snap, now):
                base = readiness_store.overlay_cached_readiness(base, snap)

            if not deep:
                rows.append(base)
                continue

            # Deep mode: skip Telegram connect when READY is still trusted.
            if (
                snap
                and snap.status == readiness_store.STAT_READY
                and readiness_store.snapshot_row_valid(snap, now)
                and getattr(snap, "checked_at", None) is not None
                and (now - snap.checked_at).total_seconds() <= readiness_store.READINESS_TRUST_WINDOW_SEC
            ):
                rows.append(base)
                continue

            rows.append(await self._readiness_row(account, deep=True))

        return rows

    async def _readiness_row(self, account: Account, deep: bool) -> Dict[str, Any]:
        display = (account.first_name or "") + (
            (" " + account.last_name) if account.last_name else ""
        )
        display = display.strip() or None

        if not deep:
            kind, err = probe_telethon_session_kind(account)
            session_exists = err is None and kind in ("file", "string")
            base: Dict[str, Any] = {
                "account_id": account.id,
                "phone": account.phone_number,
                "display_name": display,
                "session_kind": kind if kind != "empty" else "unknown",
                "session_exists": session_exists,
                "authorized": None,
                "ready": False,
                "error": err,
            }
            if err in (ERR_SESSION_FILE_MISSING, ERR_INVALID_SESSION_FORMAT, ERR_EMPTY_SESSION):
                base["ready"] = False
                base["error"] = human_message_for_code(err)
                return base
            # Session on disk ≠ logged in at Telegram. UI must not show READY until
            # a deep check proves ``is_user_authorized()`` (or operator rechecks).
            base["authorized"] = None
            base["ready"] = False
            base["error"] = None
            base["readiness_failure_kind"] = None
            base["failure_code"] = None
            return base

        session, kind, err = resolve_telethon_session(account)
        session_exists = err is None and kind in ("file", "string")

        base: Dict[str, Any] = {
            "account_id": account.id,
            "phone": account.phone_number,
            "display_name": display,
            "session_kind": kind if kind != "empty" else "unknown",
            "session_exists": session_exists,
            "authorized": None,
            "ready": False,
            "error": err,
        }

        if err in (ERR_SESSION_FILE_MISSING, ERR_INVALID_SESSION_FORMAT, ERR_EMPTY_SESSION):
            base["ready"] = False
            base["error"] = human_message_for_code(err)
            return base

        base["readiness_failure_kind"] = None
        base["failure_code"] = None

        deep_lock: Optional[SessionLockHandle] = None
        if kind == "file":
            ok_lock, lock_handle, lock_err = acquire_session_lock(
                account.id, timeout_sec=45.0,
            )
            if not ok_lock:
                base["authorized"] = None
                base["ready"] = False
                base["readiness_failure_kind"] = "transient"
                base["failure_code"] = "session_lock_timeout"
                base["error"] = human_message_for_code("session_lock_timeout")
                if lock_err:
                    base["error"] = f"{base['error']} ({lock_err})"
                return base
            deep_lock = lock_handle

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
        try:
            last_exc: Optional[BaseException] = None
            auth: Optional[bool] = None
            for attempt in range(1, _CONNECT_ATTEMPTS + 1):
                try:
                    await client.connect()
                    auth = await client.is_user_authorized()
                    last_exc = None
                    break
                except Exception as e:
                    last_exc = e
                    try:
                        if client.is_connected():
                            await client.disconnect()
                    except Exception:
                        pass
                    if attempt >= _CONNECT_ATTEMPTS:
                        fc = _connect_failure_code(last_exc)
                        transient = fc in (
                            "session_db_locked",
                            "failed_connect_network",
                            "session_lock_timeout",
                        )
                        base["authorized"] = None
                        base["ready"] = False
                        base["failure_code"] = fc
                        base["readiness_failure_kind"] = "transient" if transient else "error"
                        base["error"] = f"{human_message_for_code(fc)}: {last_exc}"
                        auth = None
                        break
                    delay = min(
                        _CONNECT_BACKOFF_CAP_S,
                        _CONNECT_BACKOFF_BASE_S * (2 ** (attempt - 1)),
                    )
                    await asyncio.sleep(delay)
            if auth is not None:
                base["authorized"] = auth
                base["ready"] = bool(auth)
                base["error"] = None if auth else human_message_for_code("unauthorized_session")
                if not auth:
                    base["readiness_failure_kind"] = "auth"
                    base["failure_code"] = "unauthorized_session"
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
            if deep_lock is not None:
                try:
                    deep_lock.release()
                except Exception:
                    pass

        return base

    async def remove_account(self, account_id: int) -> bool:
        """Remove an account from the manager"""
        async with self._lock:
            if account_id in self._clients:
                wrapper = self._clients[account_id]
                await wrapper.disconnect()
                del self._clients[account_id]
                self._account_connect_locks.pop(account_id, None)
                self._account_connect_lock_loop.pop(account_id, None)
                logger.debug("Account removed", account_id=account_id)
                return True
            return False

    async def get_client(self, account_id: int) -> Optional[TelegramClientWrapper]:
        """Get a client by account ID"""
        async with self._lock:
            await self._drop_stale_client_unlocked(account_id)
            return self._clients.get(account_id)

    async def get_available_clients(self) -> List[TelegramClientWrapper]:
        """Get all available (connected and not rate-limited) clients"""
        async with self._lock:
            for aid in list(self._clients.keys()):
                await self._drop_stale_client_unlocked(aid)
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

                _w, mgr_err = await self.add_account(account)
                if mgr_err:
                    logger.warning(
                        "Account saved but client manager could not load session",
                        account_id=account.id,
                        reason=mgr_err,
                    )

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

    async def import_session_string(self, session_string: str, **_: Any) -> Dict[str, Any]:
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
                    _w, mgr_err = await self.add_account(existing)
                    if mgr_err:
                        logger.warning(
                            "Account updated but client manager could not load session",
                            account_id=existing.id,
                            reason=mgr_err,
                        )
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
                    _w, mgr_err = await self.add_account(account)
                    if mgr_err:
                        logger.warning(
                            "Account imported but client manager could not load session",
                            account_id=account.id,
                            reason=mgr_err,
                        )
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
        async with self._lock:
            await self._drop_stale_client_unlocked(account_id)
            wrapper = self._clients.get(account_id)
        if not wrapper:
            with get_db_context() as db:
                account = db.query(Account).filter(Account.id == account_id).first()
            if account:
                wrapper, err = await self.add_account(account)
                if not wrapper:
                    return {
                        "error": human_message_for_code(err) if err else "Account not in manager",
                        "dialogs": [],
                    }
            else:
                return {"error": "Account not in manager", "dialogs": []}
        if not wrapper.is_connected:
            ok, reason = await wrapper.connect_with_reason()
            if not ok:
                return {
                    "error": human_message_for_code(reason) if reason else "Could not connect account",
                    "dialogs": [],
                }
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

    async def list_joined_groups_channels(self, account_id: int, limit: int = 300) -> Dict[str, Any]:
        """
        Read-only: groups / channels / supergroups from the account's dialog list
        (excludes private 1:1 User chats). Used by Scheduler operators to compare
        Telegram membership vs ``chat_targets`` bindings — does not mutate Telegram.

        ``limit`` is the maximum **group/channel rows** to return. Dialog iteration
        continues past that many *dialogs* because the first N dialogs are often
        dominated by DMs; we scan up to ``max_dialog_scans`` then stop (see response
        ``scan_capped``).
        """
        async with self._lock:
            await self._drop_stale_client_unlocked(account_id)
            wrapper = self._clients.get(account_id)
        if not wrapper:
            with get_db_context() as db:
                account = db.query(Account).filter(Account.id == account_id).first()
            if account:
                wrapper, err = await self.add_account(account)
                if not wrapper:
                    return {
                        "error": human_message_for_code(err) if err else "Account not in manager",
                        "groups": [],
                    }
            else:
                return {"error": "Account not found", "groups": []}
        if not wrapper.is_connected:
            ok, reason = await wrapper.connect_with_reason()
            if not ok:
                return {
                    "error": human_message_for_code(reason) if reason else "Could not connect account",
                    "groups": [],
                }
        try:
            max_groups = max(1, min(int(limit), 500))
            # Budget of raw dialogs to walk before giving up (many accounts have lots
            # of private chats ahead of older groups in recency order).
            max_dialog_scans = min(20000, max(2000, max_groups * 25))
            out: List[Dict[str, Any]] = []
            seen_peers: set = set()
            scanned = 0
            async for dialog in wrapper.client.iter_dialogs():
                scanned += 1
                entity = dialog.entity
                if not isinstance(entity, User):
                    title = (dialog.name or getattr(entity, "title", None) or "") or ""
                    username = getattr(entity, "username", None) or None
                    if isinstance(entity, Channel):
                        chat_type = "channel" if entity.broadcast else "supergroup"
                        entity_id = int(entity.id)
                    elif isinstance(entity, Chat):
                        chat_type = "group"
                        entity_id = int(entity.id)
                    else:
                        entity_id = None
                        chat_type = ""
                    if entity_id is not None:
                        peer_id = int(get_peer_id(entity))
                        if peer_id not in seen_peers:
                            seen_peers.add(peer_id)
                            out.append({
                                "title": title,
                                "username": username,
                                "tg_entity_id": entity_id,
                                "peer_id": peer_id,
                                "chat_type": chat_type,
                            })
                if len(out) >= max_groups or scanned >= max_dialog_scans:
                    break
            scan_capped = scanned >= max_dialog_scans and len(out) < max_groups
            if scan_capped:
                logger.warning(
                    "list_joined_groups_channels_scan_capped",
                    account_id=account_id,
                    groups=len(out),
                    max_groups=max_groups,
                    dialogs_scanned=scanned,
                    max_dialog_scans=max_dialog_scans,
                )
            return {
                "groups": out,
                "total": len(out),
                "dialogs_scanned": scanned,
                "scan_capped": scan_capped,
                "max_dialog_scans": max_dialog_scans,
            }
        except Exception as e:
            logger.error("list_joined_groups_channels failed", account_id=account_id, error=str(e))
            return {"error": str(e), "groups": []}


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
