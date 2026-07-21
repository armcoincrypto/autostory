"""One-operation Telegram client lifecycle for controlled Story publishing."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog
from telethon import TelegramClient

from config.settings import settings
from src.clients.manager import TelegramClientWrapper
from src.clients.rate_limiter import RateLimiter
from src.clients.session_resolve import resolve_telethon_session
from src.core.database import get_db_context
from src.core.models import Account
from src.core.session_lock import SessionLockHandle, acquire_session_lock

logger = structlog.get_logger(__name__)


@dataclass
class ControlledStoryClientLease:
    wrapper: TelegramClientWrapper
    session_lock: SessionLockHandle

    async def close(self) -> dict[str, Any]:
        """Disconnect first, then always release the account session lock."""
        errors: list[str] = []
        try:
            await self.wrapper.disconnect()
        except Exception as exc:
            errors.append(f"disconnect_failed:{type(exc).__name__}")
            logger.warning(
                "controlled_story_client_disconnect_failed",
                account_id=self.wrapper.account.id,
                error=str(exc),
            )
        try:
            self.session_lock.release()
        except Exception as exc:
            errors.append(f"session_lock_release_failed:{type(exc).__name__}")
            logger.warning(
                "controlled_story_session_lock_release_failed",
                account_id=self.wrapper.account.id,
                error=str(exc),
            )
        return {"ok": not errors, "errors": errors}


async def open_controlled_story_client(
    account_id: int,
    *,
    lock_timeout_sec: float = 30.0,
) -> tuple[ControlledStoryClientLease | None, str | None]:
    """Resolve, lock, connect, and authorize one non-pooled Story client."""
    aid = int(account_id)
    ok, lock_handle, lock_error = acquire_session_lock(
        aid,
        timeout_sec=lock_timeout_sec,
    )
    if not ok or lock_handle is None:
        return None, "session_lock_timeout"

    client: TelegramClient | None = None
    try:
        with get_db_context() as db:
            account = db.query(Account).filter(Account.id == aid).first()
        if account is None:
            lock_handle.release()
            return None, "account_not_found"

        session, _kind, resolve_error = resolve_telethon_session(account)
        if resolve_error or session is None:
            lock_handle.release()
            return None, resolve_error or "no_usable_session"

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
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            lock_handle.release()
            return None, "not_authorized"

        wrapper = TelegramClientWrapper(account, client, RateLimiter())
        wrapper.is_connected = True
        return ControlledStoryClientLease(wrapper, lock_handle), None
    except Exception as exc:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass
        lock_handle.release()
        logger.warning(
            "controlled_story_client_open_failed",
            account_id=aid,
            error_class=type(exc).__name__,
            error=str(exc),
        )
        return None, "connect_failed"

