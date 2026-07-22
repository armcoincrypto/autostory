# P9.38 draft restore — source-backed from Cursor snapshots (not byte-matched to archive .pyc).
# Do not restart scheduler until import probe + tests pass and operator approves.

"""
Telegram Client Manager - Multi-Account Orchestration
Handles concurrent user sessions using Telethon
"""
import asyncio
import contextlib
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, List, Callable, Any, Union, Tuple

from telethon import TelegramClient
from telethon.sessions import StringSession, SQLiteSession
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
from sqlalchemy.exc import IntegrityError

from src.ai_agent.account_allowlist import RESERVED_AI_AGENT_ACCOUNT_IDS
from src.core.session_paths import get_canonical_session_path, get_sessions_dir
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

BLOCKED_MSG = "blocked_by_p9_source_recovery_no_telethon_connect"

logger = structlog.get_logger(__name__)

_SESSION_LOCK_HEARTBEAT_SEC = 12.0


async def _session_lock_heartbeat_loop(handle: SessionLockHandle) -> None:
    """Update lock sidecar heartbeat while Telethon holds the flock (diagnostic only)."""
    try:
        while True:
            await asyncio.sleep(_SESSION_LOCK_HEARTBEAT_SEC)
            try:
                handle.heartbeat()
            except Exception:
                pass
    except asyncio.CancelledError:
        return

# Telethon connect retries (deep readiness only; not used by add_account).
_CONNECT_ATTEMPTS = 4
_CONNECT_BACKOFF_BASE_S = 0.5
_CONNECT_BACKOFF_CAP_S = 30.0


def _connect_failure_code(exc: Optional[BaseException]) -> str:
    """Classify Telethon connect failures for deep readiness checks."""
    if exc is None:
        return "failed_connect"
    if isinstance(exc, sqlite3.OperationalError):
        if "locked" in str(exc).lower():
            return "session_db_locked"
        return "failed_connect"
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "failed_connect_network"
    if isinstance(exc, OSError):
        return "failed_connect_network"
    msg = str(exc).lower()
    if "database is locked" in msg or "database locked" in msg:
        return "session_db_locked"
    if "server closed" in msg or "connection reset" in msg or "timed out" in msg:
        return "failed_connect_network"
    return "failed_connect"


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


def _release_wrapper_session_lock(wrapper: Any) -> None:
    """Release optional session file lock held for SQLite-backed Telethon sessions."""
    h = getattr(wrapper, "_session_lock_handle", None)
    if h is None:
        return
    try:
        h.release()
    except Exception:
        pass
    try:
        wrapper._session_lock_handle = None
    except Exception:
        pass



def _telegram_profile_mutation_restricted(err: str) -> bool:
    m = (err or "").lower()
    if "not available for frozen" in m:
        return True
    if "method that is not available" in m and "frozen" in m:
        return True
    return False


def _persist_profile_capability(account_id: int, status: str, reason: Optional[str] = None) -> None:
    with get_db_context() as db:
        acc = db.query(Account).filter(Account.id == int(account_id)).first()
        if not acc:
            return
        if hasattr(acc, "profile_capability_status"):
            acc.profile_capability_status = status
        if hasattr(acc, "profile_capability_reason"):
            acc.profile_capability_reason = (reason or "")[:255] if reason else None
        db.commit()

def _existing_session_source_for_healthcheck(account) -> tuple[object | None, str | None]:
    """Resolve Telethon session via the canonical boundary only.

    Returns: (session_source, source_kind) where source_kind is file|string|...
    """
    from src.clients.session_resolve import resolve_telethon_session

    session, kind, err = resolve_telethon_session(account)
    if err or session is None:
        return None, None
    return session, kind


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
        self._session_lock_handle: Optional[SessionLockHandle] = None


    async def connect_with_reason(self) -> Tuple[bool, Optional[str]]:
        """Connect and verify authorization. Returns (ok, failure_code)."""
        async with self._lock:
            last_err: Optional[BaseException] = None
            for attempt in range(1, _CONNECT_ATTEMPTS + 1):
                try:
                    await self.client.connect()
                    if await self.client.is_user_authorized():
                        self.is_connected = True
                        me = await self.client.get_me()
                        logger.info(
                            "Client connected",
                            phone=self.account.phone_number,
                            user_id=me.id,
                            username=me.username,
                            connect_attempt=attempt,
                        )
                        return True, None
                    logger.warning(
                        "Client not authorized",
                        phone=self.account.phone_number,
                    )
                    return False, "unauthorized_session"
                except Exception as e:
                    last_err = e
                    logger.warning(
                        "Connection attempt failed",
                        phone=self.account.phone_number,
                        error=str(e),
                        connect_attempt=attempt,
                    )
                    try:
                        if self.client.is_connected():
                            await self.client.disconnect()
                    except Exception:
                        pass
                    self.is_connected = False
                    if attempt >= _CONNECT_ATTEMPTS:
                        return False, _connect_failure_code(last_err)
                    delay = min(
                        _CONNECT_BACKOFF_CAP_S,
                        _CONNECT_BACKOFF_BASE_S * (2 ** (attempt - 1)),
                    )
                    await asyncio.sleep(delay)
            return False, _connect_failure_code(last_err)

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
                _, _ = await self.add_account(account)

        logger.info("Client manager initialized", total_clients=len(self._clients))

    async def add_account(
        self, account: Union[Account, int]
    ) -> Tuple[Optional[TelegramClientWrapper], Optional[str]]:
        """Add a new account to the manager. Accepts Account instance or account id (int).

        Acquires a session file lock when needed, connects, and releases orphan wrappers
        on cancel/connect failure so locks are not leaked.
        """
        if account is None:
            raise RuntimeError(f"{BLOCKED_MSG}: add_account")
        async with self._lock:
            if isinstance(account, int):
                with get_db_context() as db:
                    account = db.query(Account).filter(Account.id == account).first()
                    if not account or not account.session_string:
                        return None, "empty_session"
            if account.id in self._clients:
                return self._clients[account.id], None

            session_file_lock: Optional[SessionLockHandle] = None
            try:
                session, kind, err = resolve_telethon_session(
                    account, prefer_string_over_file=False,
                )
                if err or session is None:
                    code = err or "empty_session"
                    logger.warning(
                        "add_account_session_unresolved",
                        account_id=account.id,
                        session_kind=kind,
                        error_code=code,
                    )
                    return None, code

                ok_lock, lock_handle, lock_err = acquire_session_lock(account.id)
                if not ok_lock:
                    logger.warning(
                        "session_file_lock_not_acquired",
                        account_id=account.id,
                        detail=lock_err,
                    )
                    return None, "session_lock_timeout"
                session_file_lock = lock_handle

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
                wrapper._session_lock_handle = session_file_lock
                session_file_lock = None

                async def _release_orphan_wrapper(reason: str) -> None:
                    try:
                        await wrapper.disconnect()
                    except Exception:
                        pass
                    _release_wrapper_session_lock(wrapper)
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
                    return None, connect_reason or "failed_connect"

                self._clients[account.id] = wrapper
                logger.info("Account added", account_id=account.id, phone=account.phone_number)
                return wrapper, None

            except asyncio.CancelledError:
                if session_file_lock is not None:
                    try:
                        session_file_lock.release()
                    except Exception:
                        pass
                raise
            except Exception as e:
                if session_file_lock is not None:
                    try:
                        session_file_lock.release()
                    except Exception:
                        pass
                logger.error("Failed to add account", account_id=getattr(account, "id", None), error=str(e))
                return None, "failed_connect"

    async def collect_accounts_readiness(
        self, deep: bool = False, only_account_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Read-only readiness snapshot for operators.
        When deep=False: path / StringSession probe only (no SQLiteSession, no Telegram connect).
        When deep=True: connect + is_user_authorized per account.
        """
        if deep:
            raise RuntimeError(f"{BLOCKED_MSG}: collect_accounts_readiness(deep=True)")
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

            base = await self._readiness_row(account, deep=False)
            if snap and readiness_store.snapshot_row_valid(snap, now):
                base = readiness_store.overlay_cached_readiness(base, snap)

            if not deep:
                rows.append(base)
                continue

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
            base["authorized"] = None
            base["ready"] = False
            base["error"] = None
            base["readiness_failure_kind"] = None
            base["failure_code"] = None
            return base

        session, kind, err = resolve_telethon_session(
            account, prefer_string_over_file=False,
        )
        session_exists = err is None and kind in ("file", "string")

        base = {
            "account_id": account.id,
            "phone": account.phone_number,
            "display_name": display,
            "session_kind": kind if kind != "empty" else "unknown",
            "session_exists": session_exists,
            "authorized": None,
            "ready": False,
            "error": err,
        }

        if err is not None:
            base["ready"] = False
            base["session_exists"] = False
            base["error"] = human_message_for_code(err)
            base["readiness_failure_kind"] = "session_material"
            base["failure_code"] = err
            return base

        base["readiness_failure_kind"] = None
        base["failure_code"] = None

        deep_lock: Optional[SessionLockHandle] = None
        hb_task: Optional[asyncio.Task] = None
        if kind == "file":
            ok_lock, lock_handle, lock_err = acquire_session_lock(
                account.id,
                timeout_sec=45.0,
                subsystem="readiness_worker",
                operation="deep_check",
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

        if deep_lock is not None:
            hb_task = asyncio.create_task(_session_lock_heartbeat_loop(deep_lock))

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
            if hb_task is not None:
                hb_task.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await hb_task
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

    async def connect_account(
        self,
        account_id: int,
        *,
        session_lock_timeout_sec: float = 45.0,
    ) -> Tuple[Optional[TelegramClientWrapper], Optional[str]]:
        """
        Connect a Telethon client for ``account_id`` using the same session resolution
        as deep readiness (file canonical path preferred over string).

        Returns:
            (wrapper, None) when connected and ``is_user_authorized()`` is True.
            (None, reason_code) on resolver failures, lock timeout, connect failures,
            or unauthorized session. Does not send messages or mutate Account rows.
        """
        aid = int(account_id)
        with get_db_context() as db:
            account = db.query(Account).filter(Account.id == aid).first()
        if account is None:
            return None, "missing_account"

        session, kind, err = resolve_telethon_session(
            account, prefer_string_over_file=False,
        )
        if err is not None:
            return None, err

        deep_lock: Optional[SessionLockHandle] = None
        hb_task: Optional[asyncio.Task] = None
        if kind == "file":
            ok_lock, lock_handle, _lock_err = acquire_session_lock(
                aid,
                timeout_sec=float(session_lock_timeout_sec),
                subsystem="connect_account",
                operation="connect",
            )
            if not ok_lock or lock_handle is None:
                return None, "session_lock_timeout"
            deep_lock = lock_handle

        if deep_lock is not None:
            hb_task = asyncio.create_task(_session_lock_heartbeat_loop(deep_lock))

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
        last_exc: Optional[BaseException] = None
        auth: Optional[bool] = None
        try:
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
                        auth = None
                        break
                    delay = min(
                        _CONNECT_BACKOFF_CAP_S,
                        _CONNECT_BACKOFF_BASE_S * (2 ** (attempt - 1)),
                    )
                    await asyncio.sleep(delay)

            if auth is None:
                fc = _connect_failure_code(last_exc)
                return None, fc
            if not auth:
                return None, "unauthorized_session"

            wrapper = TelegramClientWrapper(account, client, self._rate_limiter)
            wrapper.is_connected = True
            wrapper._session_lock_handle = deep_lock
            deep_lock = None

            async with self._lock:
                old = self._clients.pop(aid, None)
            if old is not None:
                try:
                    await old.disconnect()
                except Exception:
                    pass
                _release_wrapper_session_lock(old)
            async with self._lock:
                self._clients[aid] = wrapper
            return wrapper, None
        finally:
            if hb_task is not None:
                hb_task.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await hb_task
            if auth is None or auth is False:
                try:
                    if client.is_connected():
                        await client.disconnect()
                except Exception:
                    pass
                if deep_lock is not None:
                    try:
                        deep_lock.release()
                    except Exception:
                        pass

    async def remove_account(self, account_id: int) -> bool:
        """Remove an account from the manager"""
        async with self._lock:
            if account_id in self._clients:
                wrapper = self._clients[account_id]
                await wrapper.disconnect()
                _release_wrapper_session_lock(wrapper)
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
            wrapper, _ = await self.add_account(account_id)
        if wrapper is None:
            return None
        if not wrapper.is_connected:
            ok = await wrapper.connect()
            if not ok:
                logger.warning("Client failed to connect", account_id=account_id)
                return None
        return wrapper

    async def get_fresh_client_for_story_publish(
        self, account_id: int
    ) -> tuple[Optional[TelegramClientWrapper], Optional[str]]:
        """
        Dedicated Telethon client for story precheck/publish, not pooled in _clients.
        Uses the same session resolution as check_accounts_health (canonical file,
        session_path file, valid session_string).

        Returns:
            (wrapper, None) on success — wrapper._precheck_disconnect_after is True;
            routes disconnect in finally after precheck.
            (None, reason_code) on failure — short machine-friendly reason, no secrets.
        """
        with get_db_context() as db:
            account = db.query(Account).filter(Account.id == account_id).first()
            if not account:
                return None, "account_not_found"

        session_source, source_kind = _existing_session_source_for_healthcheck(account)
        if session_source is None:
            return None, "no_usable_session"

        try:
            client = TelegramClient(
                session_source,
                settings.telegram.api_id,
                settings.telegram.api_hash,
                proxy=account.proxy_config if account.proxy_config else None,
                device_model="STORYFLEET",
                app_version="1.0.0",
                system_version="Linux",
                lang_code="en",
            )
        except Exception as e:
            logger.warning(
                "story_publish_client_build_failed",
                account_id=account_id,
                source_kind=source_kind,
                error=str(e),
            )
            return None, "client_build_failed"

        wrapper = TelegramClientWrapper(account, client, self._rate_limiter)
        wrapper._precheck_disconnect_after = True

        try:
            await client.connect()
            if not await client.is_user_authorized():
                try:
                    await client.disconnect()
                except Exception:
                    pass
                return None, "not_authorized"
        except Exception as e:
            try:
                await client.disconnect()
            except Exception:
                pass
            logger.warning(
                "story_publish_client_connect_failed",
                account_id=account_id,
                source_kind=source_kind,
                error=str(e),
            )
            return None, "connect_failed"

        logger.info(
            "story_publish_client_ready",
            account_id=account_id,
            session_source_kind=source_kind,
        )
        return wrapper, None

    async def get_dialogs(self, account_id: int, limit: int = 200) -> List[Dict[str, Any]]:
        """Fetch groups/channels the account is in. Returns list of {id, title, username, chat_type}."""
        with get_db_context() as db:
            account = db.query(Account).filter(Account.id == account_id).first()
            if not account or not account.session_string:
                return []
        wrapper = await self.get_client(account_id)
        if not wrapper:
            _, _ = await self.add_account(account)
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
        """Upload a profile photo; records profile_capability_* when Telegram blocks mutation APIs."""
        wrapper = await self.get_client(account_id)
        if not wrapper:
            return {"success": False, "error": "Account not available or not connected."}
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

    def healthcheck_eligible_account_ids(
        self,
        account_ids_filter: Optional[List[int]] = None,
    ) -> List[int]:
        """
        Account IDs that have a usable session source for general Telegram healthcheck
        (canonical file, session_path file, or valid session_string — same as check_accounts_health).

        Used by background fleet jobs to skip accounts that would only produce no_session rows.
        Optional account_ids_filter: if provided (non-empty), restrict to these IDs; if [],
        returns []. If None, evaluate all accounts in DB.
        """
        with get_db_context() as db:
            q = db.query(Account)
            if account_ids_filter is not None:
                if not account_ids_filter:
                    return []
                q = q.filter(Account.id.in_(account_ids_filter))
            accounts = q.order_by(Account.id).all()

        eligible: List[int] = []
        for account in accounts:
            session_source, _ = _existing_session_source_for_healthcheck(account)
            if session_source is not None:
                eligible.append(account.id)
        return eligible

    async def check_accounts_health(
        self,
        update_status: bool = False,
        account_ids: Optional[List[int]] = None,
        verbose: bool = False,
        persist: bool = True,
        **kwargs
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
        results: List[Dict[str, Any]] = []
        with get_db_context() as db:
            q = db.query(Account)
            if account_ids is not None:
                q = q.filter(Account.id.in_(account_ids))
            accounts = q.all()

        total_accounts = len(accounts)
        progress_callback = kwargs.get("progress_callback")
        local_checked = 0

        def _append_result(row: Dict[str, Any]) -> None:
            nonlocal local_checked
            results.append(row)
            local_checked += 1
            if callable(progress_callback):
                progress_callback(local_checked, total_accounts, row)

        for account in accounts:
            account_id = account.id
            phone = getattr(account, "phone_number", None)
            username = getattr(account, "username", None)
            _status = getattr(account, "status", None)
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

            session_source, session_source_kind = _existing_session_source_for_healthcheck(account)
            if session_source is None:
                _append_result({
                    "account_id": account_id,
                    "phone": phone or f"#{account_id}",
                    "username": username,
                    "status": "error",
                    "message": "No usable session",
                    "reason_code": "no_session",
                    "checked_at": checked_at,
                })
                if update_status:
                    with get_db_context() as db:
                        acc = db.query(Account).filter(Account.id == account_id).first()
                        if acc:
                            acc.status = AccountStatus.AUTH_REQUIRED
                continue

            if verbose:
                logger.info(
                    "alive_check_session_source",
                    account_id=account_id,
                    source=session_source_kind,
                )

            client = TelegramClient(
                session_source,
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
                    _append_result({
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
                    _append_result({
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
                    _append_result({
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
                _append_result({
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
                _append_result({
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
                _append_result({
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
                _append_result({
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
                _append_result({
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
                _append_result({
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
                _append_result({
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
                _append_result({
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
                _, _ = await self.add_account(account)

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
                    _, _ = await self.add_account(existing)
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
                _, _ = await self.add_account(account)
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


# Pending QR logins: token -> state dict (new account or repair file).
_pending_qr: Dict[str, Dict[str, Any]] = {}
_qr_lock = threading.Lock()


def _normalize_e164_digits(phone: Optional[str]) -> str:
    if not phone:
        return ""
    return "".join(c for c in str(phone).strip() if c.isdigit())


def _phone_string_from_tl_user(me: Any) -> str:
    """E.164-ish ``+digits`` from Telethon / TL User."""
    p = getattr(me, "phone", None)
    if p is None:
        return ""
    digits = "".join(c for c in str(p) if c.isdigit())
    return f"+{digits}" if digits else ""


def verify_qr_repair_identity(me: Any, account: Account) -> Tuple[bool, str]:
    """
    Decide whether the QR-scanned Telegram user may be written to ``account_<id>.new.session``.

    - If ``account.user_id`` is set, it must match ``me.id``.
    - Else require non-empty phone on both sides and identical digit-normalized numbers.
    """
    me_id = int(getattr(me, "id", 0) or 0)
    db_uid = getattr(account, "user_id", None)
    if db_uid is not None:
        if int(db_uid) != me_id:
            return False, "Telegram user id does not match this account (wrong account scanned)."
        return True, ""

    logged = _phone_string_from_tl_user(me)
    db_raw = getattr(account, "phone_number", None) or ""
    ld = _normalize_e164_digits(logged)
    dd = _normalize_e164_digits(db_raw)
    if ld and dd and ld == dd:
        return True, ""
    if not ld:
        return False, "Cannot verify identity: account.user_id unset and Telegram user has no phone."
    return False, "Phone from Telegram does not match account row (and user_id unset)."


def _qr_repair_validate_start(repair_account_id: Optional[int]) -> Optional[str]:
    """Return human-readable error or None when repair request may proceed."""
    if repair_account_id is None:
        return None
    aid = int(repair_account_id)
    if aid in RESERVED_AI_AGENT_ACCOUNT_IDS:
        return (
            "QR session repair is disabled for reserved controller accounts "
            f"{sorted(RESERVED_AI_AGENT_ACCOUNT_IDS)}."
        )
    with get_db_context() as db:
        if db.get(Account, aid) is None:
            return f"Account #{aid} not found."
    return None


def _qr_login_thread(token: str, repair_account_id: Optional[int] = None) -> None:
    """Background thread: QR login → new Account OR ``account_<id>.new.session`` repair file."""

    async def _run():
        client = None
        new_session_path: Optional[Path] = None
        try:
            api_id = int(settings.telegram.api_id)
            api_hash = (settings.telegram.api_hash or "").strip()
            if not api_id or not api_hash:
                raise RuntimeError("Missing TELEGRAM_API_ID / TELEGRAM_API_HASH.")

            if repair_account_id is not None:
                aid = int(repair_account_id)
                with get_db_context() as db:
                    acc_row = db.get(Account, aid)
                    if acc_row is None:
                        raise RuntimeError(f"Account #{aid} not found.")
                new_session_path = get_sessions_dir() / f"account_{aid}.new.session"
                if new_session_path.exists():
                    raise RuntimeError(
                        "A repair session file already exists — remove or rename it first: "
                        + str(new_session_path)
                    )
                new_session_path.parent.mkdir(parents=True, exist_ok=True)
                sqlite_base = str(new_session_path.with_suffix(""))
                session = SQLiteSession(sqlite_base)
                client = TelegramClient(session, api_id, api_hash)
                await client.connect()
                if await client.is_user_authorized():
                    raise RuntimeError(
                        "Target path already has an authorized session — delete stale "
                        + str(new_session_path)
                        + " first."
                    )
                qr = await client.qr_login()
                with _qr_lock:
                    _pending_qr[token]["url"] = qr.url
                    _pending_qr[token]["status"] = "waiting"
                user = await qr.wait(timeout=120)
                with get_db_context() as db:
                    acc2 = db.get(Account, aid)
                    if acc2 is None:
                        if new_session_path.is_file():
                            new_session_path.unlink()
                        raise RuntimeError(f"Account #{aid} not found after scan.")
                    ok, verr = verify_qr_repair_identity(user, acc2)
                    if not ok:
                        if new_session_path.is_file():
                            new_session_path.unlink()
                        raise RuntimeError(verr)
                with _qr_lock:
                    _pending_qr[token]["status"] = "success"
                    _pending_qr[token]["account_id"] = aid
                    _pending_qr[token]["mode"] = "repair"
                    _pending_qr[token]["new_session_path"] = str(new_session_path.resolve())
                    _pending_qr[token]["repair_message"] = (
                        f"Repair file created for account #{aid}. Primary session unchanged. "
                        "Next: dry-run ``regenerate_telethon_session.py`` then ``--execute`` if compatible. "
                        "See scripts/ops/P8_7_6_SESSION_REGENERATION.md."
                    )
                return

            session = StringSession()
            client = TelegramClient(session, api_id, api_hash)
            await client.connect()
            qr = await client.qr_login()
            with _qr_lock:
                _pending_qr[token]["url"] = qr.url
                _pending_qr[token]["status"] = "waiting"
            await qr.wait(timeout=120)
            me = await client.get_me()
            phone = f"+{me.phone}" if me.phone else f"user_{me.id}"
            with get_db_context() as db:
                existing = db.query(Account).filter(Account.phone_number == phone).first()
                if existing is not None:
                    raise RuntimeError(
                        f"This phone already exists as account #{existing.id}. "
                        f"Use **Repair session via QR** on the Accounts page for account #{existing.id} instead."
                    )
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
                try:
                    db.commit()
                except IntegrityError:
                    db.rollback()
                    dup = db.query(Account).filter(Account.phone_number == phone).first()
                    dup_id = dup.id if dup else "?"
                    raise RuntimeError(
                        f"This phone already exists as account #{dup_id}. "
                        f"Use **Repair session via QR** for account #{dup_id} instead."
                    )
                db.refresh(acc)
            _, _ = await client_manager.add_account(acc)
            with _qr_lock:
                _pending_qr[token]["status"] = "success"
                _pending_qr[token]["account_id"] = acc.id
                _pending_qr[token]["mode"] = "new_account"
        except asyncio.TimeoutError:
            with _qr_lock:
                _pending_qr[token]["status"] = "expired"
                _pending_qr[token]["error"] = "QR code expired. Generate a new one."
        except SessionPasswordNeededError:
            with _qr_lock:
                _pending_qr[token]["status"] = "error"
                _pending_qr[token]["error"] = (
                    "2FA password required. Use Import from tdata, Phone+Code, or CLI session tool instead."
                )
            if new_session_path and new_session_path.is_file():
                try:
                    new_session_path.unlink()
                except OSError:
                    pass
        except Exception as e:
            with _qr_lock:
                _pending_qr[token]["status"] = "error"
                _pending_qr[token]["error"] = str(e)
            if new_session_path and new_session_path.is_file():
                try:
                    new_session_path.unlink()
                except OSError:
                    pass
        finally:
            if client:
                await client.disconnect()

    asyncio.run(_run())


def start_qr_login(repair_account_id: Optional[int] = None) -> Dict[str, Any]:
    """Start QR login. ``repair_account_id`` writes ``account_<id>.new.session`` only (no INSERT)."""
    api_id = getattr(settings.telegram, "api_id", None) or int(
        __import__("os").environ.get("TELEGRAM_API_ID", 0) or 0
    )
    api_hash = getattr(settings.telegram, "api_hash", None) or __import__(
        "os"
    ).environ.get("TELEGRAM_API_HASH", "")
    if not api_id or not api_hash:
        return {
            "success": False,
            "error": "TELEGRAM_API_ID and TELEGRAM_API_HASH required. Run sync_db_from_server with --env.",
        }
    pre = _qr_repair_validate_start(repair_account_id)
    if pre:
        return {"success": False, "error": pre}
    token = str(uuid.uuid4())
    with _qr_lock:
        _pending_qr[token] = {
            "url": None,
            "status": "starting",
            "repair_account_id": repair_account_id,
        }
    t = threading.Thread(target=_qr_login_thread, args=(token, repair_account_id))
    t.daemon = True
    t.start()
    for _ in range(50):
        time.sleep(0.2)
        with _qr_lock:
            if _pending_qr[token].get("url"):
                out: Dict[str, Any] = {
                    "success": True,
                    "token": token,
                    "url": _pending_qr[token]["url"],
                }
                if repair_account_id is not None:
                    out["mode"] = "repair"
                    out["repair_account_id"] = int(repair_account_id)
                return out
            if _pending_qr[token].get("status") == "error":
                return {"success": False, "error": _pending_qr[token].get("error", "Unknown error")}
    return {"success": False, "error": "QR login failed to start"}


def check_qr_login(token: str) -> Dict[str, Any]:
    """Poll QR login status (new account or repair file)."""
    with _qr_lock:
        if token not in _pending_qr:
            return {"success": False, "error": "Invalid or expired token"}
        p = _pending_qr[token]
        if p["status"] == "success":
            out: Dict[str, Any] = {
                "success": True,
                "account_id": p.get("account_id"),
                "mode": p.get("mode", "new_account"),
            }
            if p.get("mode") == "repair":
                out["new_session_path"] = p.get("new_session_path")
                out["message"] = p.get("repair_message", "Repair session file created.")
                out["next_steps"] = (
                    "Dry-run: ./venv/bin/python scripts/ops/regenerate_telethon_session.py "
                    f"--account-id {p.get('account_id')} --new-session-file "
                    f"{p.get('new_session_path') or 'data/sessions/account_<id>.new.session'} ; "
                    "only add ``--execute`` if compatible."
                )
            return out
        if p["status"] in ("error", "expired"):
            return {"success": False, "error": p.get("error", "Login failed")}
        return {"success": False, "status": "waiting"}


# Global client manager instance
client_manager = ClientManager()
