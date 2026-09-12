"""Policy-free Telegram DM transport (Wave 6A).

Does not enforce AI allowlist or owner Messages policy.
Callers must apply product policy before invoking.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional, Protocol

import structlog

from src.clients.manager import client_manager
from src.core.database import get_db_context
from src.core.models import Account, AccountStatus
from src.messaging.errors import map_dm_error
from src.messaging.peer_ids import normalize_peer_target

logger = structlog.get_logger(__name__)


class DmTransport(Protocol):
    async def send_message_async(
        self,
        account_id: int,
        target: str,
        text: str,
        peer_type: str = "private",
    ) -> dict[str, Any]: ...

    async def fetch_recent_messages_async(
        self,
        account_id: int,
        target: str,
        limit: int,
        peer_type: str = "private",
    ) -> dict[str, Any]: ...


async def _resolve_peer(client: Any, target: str) -> Any:
    raw = (target or "").strip()
    if not raw:
        raise ValueError("empty_target")
    if raw.startswith("@"):
        handle = raw[1:].strip()
        if handle.isdigit():
            return await client.get_entity(int(handle))
        return await client.get_entity(handle)
    if raw.isdigit() or (raw.startswith("-") and raw[1:].isdigit()):
        return await client.get_entity(int(raw))
    return await client.get_entity(raw)


async def _resolve_peer_with_dialog_warm(
    account_id: int,
    client: Any,
    target: str,
    *,
    peer_type: str | None = None,
) -> Any:
    """
    Resolve a peer for history/send.

    Cold numeric IDs often lack Telethon entity cache / access_hash. Warming via
    server-side get_dialogs (never exposing access_hash to the browser) populates
    the in-process entity cache on this client, then resolve retries.

    Group/channel bare ids from dialogs/resolve are marked via peer_type so
    Telethon does not treat them as PeerUser.
    """
    resolved_target = normalize_peer_target(target, peer_type)
    try:
        return await _resolve_peer(client, resolved_target)
    except Exception as first:
        raw = (resolved_target or "").strip()
        # Username resolves online; still warm once if get_entity failed transiently.
        logger.info(
            "dm_peer_resolve_warm_dialogs",
            account_id=int(account_id),
            peer_kind=("username" if raw.startswith("@") or not raw.lstrip("-").isdigit() else "numeric"),
            peer_type=(peer_type or "private"),
            error=str(first)[:160],
        )
        try:
            await client_manager.get_dialogs(int(account_id), limit=200)
        except Exception as warm_err:
            logger.warning(
                "dm_peer_warm_dialogs_failed",
                account_id=int(account_id),
                error=str(warm_err)[:160],
            )
            raise first
        return await _resolve_peer(client, resolved_target)


class TelegramDmTransport:
    """In-process Telethon send/fetch without product allowlists."""

    async def fetch_recent_messages_async(
        self,
        account_id: int,
        target: str,
        limit: int = 20,
        peer_type: str = "private",
    ) -> dict[str, Any]:
        lim = max(1, min(int(limit or 20), 100))
        with get_db_context() as db:
            account = db.query(Account).filter(Account.id == int(account_id)).first()
            if not account:
                return {
                    "ok": False,
                    "messages": [],
                    "error_code": "ACCOUNT_NOT_FOUND",
                    "error_message": "Account not found",
                }
            db.expunge(account)

        wrapper = await client_manager.get_client(int(account_id))
        if not wrapper:
            wrapper, err = await client_manager.add_account(account)
            if not wrapper:
                return {
                    "ok": False,
                    "messages": [],
                    "error_code": "AUTH_REQUIRED",
                    "error_message": str(err or "connect_failed"),
                }
        if not wrapper.is_connected:
            ok, reason = await wrapper.connect_with_reason()
            if not ok:
                return {
                    "ok": False,
                    "messages": [],
                    "error_code": "AUTH_REQUIRED",
                    "error_message": str(reason or "connect_failed"),
                }

        try:
            entity = await _resolve_peer_with_dialog_warm(
                int(account_id),
                wrapper.client,
                target,
                peer_type=peer_type,
            )
        except Exception as e:
            code, msg, _ = map_dm_error(e)
            # Owner-safe wording for common cold-entity misses
            if "entity" in str(e).lower() or code in {"PEER_INVALID", "USERNAME_NOT_FOUND", "UNKNOWN"}:
                if code == "UNKNOWN":
                    code = "PEER_INVALID"
                msg = "Unable to resolve this conversation. Re-open dialogs and try again."
            return {"ok": False, "messages": [], "error_code": code, "error_message": msg}

        out: list[dict[str, Any]] = []
        try:
            async for m in wrapper.client.iter_messages(entity, limit=lim):
                text = getattr(m, "message", None) or getattr(m, "text", None) or ""
                date = getattr(m, "date", None)
                ts = None
                if isinstance(date, datetime):
                    if date.tzinfo is None:
                        ts = date.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
                    else:
                        ts = date.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                out.append(
                    {
                        "message_id": getattr(m, "id", None),
                        "text": str(text)[:500],
                        "timestamp": ts,
                        "is_outgoing": bool(getattr(m, "out", False)),
                    }
                )
        except Exception as e:
            code, msg, _ = map_dm_error(e)
            return {"ok": False, "messages": [], "error_code": code, "error_message": msg}
        return {"ok": True, "messages": out}

    async def send_message_async(
        self,
        account_id: int,
        target: str,
        text: str,
        peer_type: str = "private",
    ) -> dict[str, Any]:
        """Low-level send. Caller must have already enforced product policy."""
        if not (text or "").strip():
            return {
                "ok": False,
                "success": False,
                "telegram_message_id": None,
                "sent_at": None,
                "error_code": "EMPTY_MESSAGE",
                "error_message": "Message text is empty",
                "retry_after": None,
            }

        with get_db_context() as db:
            account = db.query(Account).filter(Account.id == int(account_id)).first()
            if not account:
                return {
                    "ok": False,
                    "success": False,
                    "telegram_message_id": None,
                    "sent_at": None,
                    "error_code": "ACCOUNT_NOT_FOUND",
                    "error_message": "Account not found",
                    "retry_after": None,
                }
            if account.status != AccountStatus.ACTIVE:
                st = getattr(account.status, "value", account.status)
                return {
                    "ok": False,
                    "success": False,
                    "telegram_message_id": None,
                    "sent_at": None,
                    "error_code": "ACCOUNT_INELIGIBLE",
                    "error_message": f"Account status is {st}",
                    "retry_after": None,
                }
            db.expunge(account)

        wrapper = await client_manager.get_client(int(account_id))
        if not wrapper:
            wrapper, err = await client_manager.add_account(account)
            if not wrapper:
                return {
                    "ok": False,
                    "success": False,
                    "telegram_message_id": None,
                    "sent_at": None,
                    "error_code": "AUTH_REQUIRED",
                    "error_message": str(err or "connect_failed"),
                    "retry_after": None,
                }
        if not wrapper.is_connected:
            ok, reason = await wrapper.connect_with_reason()
            if not ok:
                return {
                    "ok": False,
                    "success": False,
                    "telegram_message_id": None,
                    "sent_at": None,
                    "error_code": "AUTH_REQUIRED",
                    "error_message": str(reason or "connect_failed"),
                    "retry_after": None,
                }

        try:
            entity = await _resolve_peer_with_dialog_warm(
                int(account_id),
                wrapper.client,
                target,
                peer_type=peer_type,
            )
        except Exception as e:
            code, msg, extras = map_dm_error(e)
            return {
                "ok": False,
                "success": False,
                "telegram_message_id": None,
                "sent_at": None,
                "error_code": code,
                "error_message": msg,
                "retry_after": extras.get("retry_after"),
            }

        try:
            sent = await wrapper.execute(wrapper.client.send_message, entity, text)
            mid = getattr(sent, "id", None)
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            logger.info(
                "owner_dm_transport_send_ok",
                account_id=int(account_id),
                telegram_message_id=mid,
            )
            return {
                "ok": True,
                "success": True,
                "telegram_message_id": int(mid) if mid is not None else None,
                "sent_at": now,
                "error_code": None,
                "error_message": None,
                "retry_after": None,
            }
        except Exception as e:
            code, msg, extras = map_dm_error(e)
            logger.warning(
                "owner_dm_transport_send_failed",
                account_id=int(account_id),
                error_code=code,
            )
            return {
                "ok": False,
                "success": False,
                "telegram_message_id": None,
                "sent_at": None,
                "error_code": code,
                "error_message": msg,
                "retry_after": extras.get("retry_after"),
            }


class CountingFakeTransport:
    """Test double: never talks to Telegram; counts send attempts."""

    def __init__(
        self,
        *,
        send_result: Optional[dict[str, Any]] = None,
        raise_on_send: Optional[Exception] = None,
    ) -> None:
        self.send_calls = 0
        self.fetch_calls = 0
        self._send_result = send_result
        self._raise_on_send = raise_on_send

    async def send_message_async(
        self,
        account_id: int,
        target: str,
        text: str,
        peer_type: str = "private",
    ) -> dict[str, Any]:
        self.send_calls += 1
        self.last_peer_type = peer_type
        self.last_target = target
        if self._raise_on_send is not None:
            raise self._raise_on_send
        if self._send_result is not None:
            return dict(self._send_result)
        return {
            "ok": True,
            "success": True,
            "telegram_message_id": 9000 + self.send_calls,
            "sent_at": datetime.now(timezone.utc).replace(tzinfo=None),
            "error_code": None,
            "error_message": None,
            "retry_after": None,
        }

    async def fetch_recent_messages_async(
        self,
        account_id: int,
        target: str,
        limit: int,
        peer_type: str = "private",
    ) -> dict[str, Any]:
        self.fetch_calls += 1
        self.last_peer_type = peer_type
        self.last_target = target
        return {"ok": True, "messages": []}
