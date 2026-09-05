"""
Single-target, single-message Telegram send for AI Agent (human-in-the-loop).

Default: jobs go through ``TelegramGatewayClient`` (DB queue + dedicated worker)
so Gunicorn does not open Telethon sessions.

Fallback: ``AI_AGENT_USE_TELEGRAM_GATEWAY=false`` uses ``TelegramDirectTransport``
(ClientManager in-process).

Never logs session_string or session file contents.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
from typing import Any

import structlog
from sqlalchemy.exc import OperationalError as SAOperationalError

from src.clients.manager import client_manager
from src.core.database import get_db_context
from src.core.models import Account, AccountStatus
from src.scheduler.executor import _map_error
from src.telegram_gateway.client import (
    TelegramGatewayClient,
    apply_fetch_wait_result,
    apply_send_wait_result,
)

logger = structlog.get_logger(__name__)


def _is_sqlite_busy_exception(exc: Exception) -> bool:
    parts = [str(exc)]
    orig = getattr(exc, "orig", None)
    if orig is not None:
        parts.append(str(orig))
    m = " ".join(parts).lower()
    return "database is locked" in m or "database is busy" in m


def _ai_agent_account_gate_passes(account_id: int) -> bool:
    from src.ai_agent.account_allowlist import account_id_permitted_for_ai_agent_tasks

    with get_db_context() as db:
        return account_id_permitted_for_ai_agent_tasks(db, int(account_id))

_gateway_enabled_logged_lock = threading.Lock()
_gateway_enabled_logged = False


def _log_gateway_enabled_once() -> None:
    global _gateway_enabled_logged
    with _gateway_enabled_logged_lock:
        if _gateway_enabled_logged:
            return
        _gateway_enabled_logged = True
        logger.info("ai_agent_gateway_enabled", ai_agent_use_telegram_gateway=True)


def _run_async(coro):
    """Run async coroutine from sync Flask/Gunicorn worker (same pattern as dashboard)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _gateway_enabled() -> bool:
    raw = os.environ.get("AI_AGENT_USE_TELEGRAM_GATEWAY", "true").strip().lower()
    return raw in ("1", "true", "yes", "on")


async def _resolve_dm_entity(client: Any, target: str) -> Any:
    """Resolve a DM target: numeric user id or @username / username string."""
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


class TelegramDirectTransport:
    """
    In-process Telethon via ClientManager (gateway worker or AI Agent direct fallback).

    Product policy notes (Wave 6A):
    - Does NOT enforce AI Agent allowlisting — that stays on ``TelegramSingleSender``.
    - Does enforce ``ACTION_TELEGRAM_SEND`` (scheduler mutations / certification scopes).
    - Owner Messages must use ``src.messaging.transport.TelegramDmTransport`` +
      ``OwnerDirectMessageService`` (MESSAGES_EXECUTION_ENABLED), not this class.
    """

    async def fetch_recent_messages_async(
        self, account_id: int, target: str, limit: int
    ) -> dict[str, Any]:
        lim = max(1, min(int(limit or 20), 100))
        raw = (target or "").strip()
        if not raw:
            return {
                "ok": False,
                "messages": [],
                "error_code": "empty_target",
                "error_message": "Target is empty",
            }

        with get_db_context() as db:
            account = db.query(Account).filter(Account.id == int(account_id)).first()
            if not account:
                logger.warning("ai_agent_fetch_account_missing", account_id=account_id)
                return {
                    "ok": False,
                    "messages": [],
                    "error_code": "account_not_found",
                    "error_message": "Account not found",
                }
            if account.status != AccountStatus.ACTIVE:
                st = getattr(account.status, "value", account.status)
                return {
                    "ok": False,
                    "messages": [],
                    "error_code": "account_not_active",
                    "error_message": f"Account status is {st}, must be active",
                }
            db.expunge(account)

        wrapper = await client_manager.get_client(int(account_id))
        if not wrapper:
            wrapper, err = await client_manager.add_account(account)
            if not wrapper:
                logger.warning(
                    "ai_agent_fetch_connect_failed",
                    account_id=account_id,
                    error_code=err,
                )
                return {
                    "ok": False,
                    "messages": [],
                    "error_code": str(err or "connect_failed"),
                    "error_message": "Could not connect Telegram client for this account",
                }

        if not wrapper.is_connected:
            ok, reason = await wrapper.connect_with_reason()
            if not ok:
                logger.warning(
                    "ai_agent_fetch_not_authorized",
                    account_id=account_id,
                    reason=reason,
                )
                return {
                    "ok": False,
                    "messages": [],
                    "error_code": str(reason or "connect_failed"),
                    "error_message": "Telegram session not authorized or connect failed",
                }

        try:
            entity = await _resolve_dm_entity(wrapper.client, raw)
        except Exception as e:
            code, msg = _map_error(e)
            logger.warning(
                "ai_agent_fetch_resolve_failed",
                account_id=account_id,
                error_code=code,
            )
            return {
                "ok": False,
                "messages": [],
                "error_code": code,
                "error_message": msg,
            }

        out: list[dict[str, Any]] = []
        try:
            async for m in wrapper.client.iter_messages(entity, limit=lim):
                tid = getattr(m, "id", None)
                if tid is None:
                    continue
                txt = getattr(m, "message", None) or getattr(m, "text", None) or ""
                if not str(txt).strip() and getattr(m, "media", None):
                    txt = "[media or non-text message]"
                dt = getattr(m, "date", None)
                date_str = ""
                if dt is not None and hasattr(dt, "isoformat"):
                    date_str = dt.isoformat()
                else:
                    date_str = str(dt or "")
                sid = getattr(m, "sender_id", None)
                out.append(
                    {
                        "telegram_message_id": int(tid),
                        "date": date_str,
                        "text": str(txt) if txt is not None else "",
                        "sender_id": int(sid) if sid is not None else None,
                        "is_outgoing": bool(getattr(m, "out", False)),
                    }
                )
        except Exception as e:
            code, msg = _map_error(e)
            logger.warning(
                "ai_agent_fetch_iter_failed",
                account_id=account_id,
                error_code=code,
            )
            return {
                "ok": False,
                "messages": [],
                "error_code": code,
                "error_message": msg,
            }

        logger.info(
            "ai_agent_fetch_ok",
            account_id=account_id,
            message_count=len(out),
        )
        return {"ok": True, "messages": out}

    async def send_message_async(
        self,
        account_id: int,
        target: str,
        text: str,
        *,
        target_id: int | None = None,
        job_marker: str | None = None,
        job_id: int | None = None,
        binding_id: int | None = None,
        content_sha256: str | None = None,
        db=None,
    ) -> dict[str, Any]:
        if not (text or "").strip():
            return {
                "ok": False,
                "telegram_message_id": None,
                "error_code": "empty_message",
                "error_message": "Message text is empty",
            }

        from src.core.execution_guard import (
            ACTION_TELEGRAM_SEND,
            guard_blocked_ai_send,
            require_execution_allowed,
        )

        # Pass certification scope through so gateway → transport does not strip
        # P6.4/P5C/P5D authorization (guard would otherwise deny under NO_GO/mutations).
        guard_db = db
        if guard_db is None and (
            job_marker or target_id is not None or binding_id is not None or content_sha256
        ):
            # Open a short-lived session so send-time binding guard can run.
            with get_db_context() as _gdb:
                blocked = require_execution_allowed(
                    ACTION_TELEGRAM_SEND,
                    account_id=int(account_id),
                    target_id=int(target_id) if target_id is not None else None,
                    job_marker=job_marker,
                    job_id=int(job_id) if job_id is not None else None,
                    binding_id=int(binding_id) if binding_id is not None else None,
                    content_sha256=content_sha256,
                    db=_gdb,
                )
        else:
            blocked = require_execution_allowed(
                ACTION_TELEGRAM_SEND,
                account_id=int(account_id),
                target_id=int(target_id) if target_id is not None else None,
                job_marker=job_marker,
                job_id=int(job_id) if job_id is not None else None,
                binding_id=int(binding_id) if binding_id is not None else None,
                content_sha256=content_sha256,
                db=guard_db,
            )
        if blocked is not None:
            logger.warning(
                "ai_agent_send_blocked_execution_guard",
                account_id=int(account_id),
                reason=blocked.reason_code,
            )
            return guard_blocked_ai_send(blocked)

        with get_db_context() as db:
            account = db.query(Account).filter(Account.id == int(account_id)).first()
            if not account:
                logger.warning("ai_agent_send_account_missing", account_id=account_id)
                return {
                    "ok": False,
                    "telegram_message_id": None,
                    "error_code": "account_not_found",
                    "error_message": "Account not found",
                }
            if account.status != AccountStatus.ACTIVE:
                st = getattr(account.status, "value", account.status)
                return {
                    "ok": False,
                    "telegram_message_id": None,
                    "error_code": "account_not_active",
                    "error_message": f"Account status is {st}, must be active to send",
                }
            db.expunge(account)

        wrapper = await client_manager.get_client(int(account_id))
        if not wrapper:
            wrapper, err = await client_manager.add_account(account)
            if not wrapper:
                logger.warning(
                    "ai_agent_send_connect_failed",
                    account_id=account_id,
                    error_code=err,
                )
                return {
                    "ok": False,
                    "telegram_message_id": None,
                    "error_code": str(err or "connect_failed"),
                    "error_message": "Could not connect Telegram client for this account",
                }

        if not wrapper.is_connected:
            ok, reason = await wrapper.connect_with_reason()
            if not ok:
                logger.warning(
                    "ai_agent_send_not_authorized",
                    account_id=account_id,
                    reason=reason,
                )
                return {
                    "ok": False,
                    "telegram_message_id": None,
                    "error_code": str(reason or "connect_failed"),
                    "error_message": "Telegram session not authorized or connect failed",
                }

        try:
            entity = await _resolve_dm_entity(wrapper.client, target)
        except Exception as e:
            code, msg = _map_error(e)
            logger.warning(
                "ai_agent_send_resolve_failed",
                account_id=account_id,
                error_code=code,
            )
            return {
                "ok": False,
                "telegram_message_id": None,
                "error_code": code,
                "error_message": msg,
            }

        try:
            sent = await wrapper.execute(
                wrapper.client.send_message,
                entity,
                text,
            )
            mid = getattr(sent, "id", None)
            logger.info(
                "ai_agent_send_ok",
                account_id=account_id,
                telegram_message_id=mid,
            )
            return {
                "ok": True,
                "telegram_message_id": int(mid) if mid is not None else None,
                "error_code": None,
                "error_message": None,
            }
        except Exception as e:
            code, msg = _map_error(e)
            logger.warning(
                "ai_agent_send_failed",
                account_id=account_id,
                error_code=code,
            )
            return {
                "ok": False,
                "telegram_message_id": None,
                "error_code": code,
                "error_message": msg,
            }


class TelegramSingleSender:
    """
    Public entry for AI Agent: gateway queue (default) or direct Telethon.

    AI Agent account allowlisting is enforced HERE (policy layer), not inside
    ``TelegramDirectTransport`` / common Telethon primitives.

    When ``AI_AGENT_USE_TELEGRAM_GATEWAY`` is true, Telethon is never used from
    this class: only ``TelegramGatewayClient`` enqueue + wait.
    """

    def __init__(self) -> None:
        self._direct: TelegramDirectTransport | None = None
        self._gateway = None

    def _get_direct(self) -> TelegramDirectTransport:
        if self._direct is None:
            self._direct = TelegramDirectTransport()
        return self._direct

    def _gw(self):
        if self._gateway is None:
            self._gateway = TelegramGatewayClient()
        return self._gateway

    def fetch_recent_messages(
        self, account_id: int, target: str, limit: int = 20
    ) -> dict[str, Any]:
        from src.ai_agent.account_allowlist import (
            account_id_may_ai_agent_inbound_fetch,
            log_ai_agent_account_skipped_once,
        )

        with get_db_context() as db:
            if not account_id_may_ai_agent_inbound_fetch(db, int(account_id)):
                log_ai_agent_account_skipped_once(
                    int(account_id), reason="inbound_fetch_not_allowed"
                )
                return {
                    "ok": False,
                    "messages": [],
                    "error_code": "forbidden_account",
                    "error_message": (
                        "Inbound Telegram sync is not allowed for this account. "
                        "Use an AI Agent line (allowlisted / gateway pool)."
                    ),
                }
        if not _ai_agent_account_gate_passes(account_id):
            logger.warning(
                "ai_agent_telegram_account_forbidden",
                account_id=int(account_id),
                path="fetch",
            )
            return {
                "ok": False,
                "messages": [],
                "error_code": "forbidden_account",
                "error_message": (
                    "This account is not reserved for AI Agent. "
                    "Choose a dedicated AI Agent account."
                ),
            }
        if _gateway_enabled():
            _log_gateway_enabled_once()
            try:
                gw = self._gw()
            except Exception as e:
                logger.warning(
                    "ai_agent_gateway_client_init_failed",
                    error=str(e),
                    path="fetch",
                )
                return {
                    "ok": False,
                    "messages": [],
                    "error_code": "gateway_error",
                    "error_message": "Telegram gateway client unavailable",
                    "transient": True,
                }
            try:
                jid = gw.enqueue_fetch_messages(account_id, target, limit)
                logger.info(
                    "ai_agent_gateway_enqueue_fetch",
                    account_id=int(account_id),
                    job_id=int(jid),
                    limit=int(limit),
                )
            except (SAOperationalError, sqlite3.OperationalError) as e:
                if _is_sqlite_busy_exception(e):
                    logger.warning(
                        "ai_agent_gateway_enqueue_database_busy",
                        path="fetch",
                        error=str(e),
                    )
                    return {
                        "ok": False,
                        "messages": [],
                        "error_code": "database_busy",
                        "error_message": "SQLite database is busy; retry shortly.",
                        "retryable": True,
                        "transient": True,
                    }
                logger.warning(
                    "ai_agent_gateway_enqueue_failed",
                    error=str(e),
                    path="fetch",
                )
                return {
                    "ok": False,
                    "messages": [],
                    "error_code": "gateway_error",
                    "error_message": "Could not enqueue Telegram gateway job",
                    "transient": True,
                }
            except Exception as e:
                logger.warning(
                    "ai_agent_gateway_enqueue_failed",
                    error=str(e),
                    path="fetch",
                )
                return {
                    "ok": False,
                    "messages": [],
                    "error_code": "gateway_error",
                    "error_message": "Could not enqueue Telegram gateway job",
                    "transient": True,
                }
            return apply_fetch_wait_result(gw.wait_for_job(jid))
        return _run_async(
            self._get_direct().fetch_recent_messages_async(
                account_id, target, limit
            )
        )

    def send_message(self, account_id: int, target: str, text: str) -> dict[str, Any]:
        from src.core.execution_guard import (
            ACTION_TELEGRAM_SEND,
            guard_blocked_ai_send,
            require_execution_allowed,
        )

        blocked = require_execution_allowed(ACTION_TELEGRAM_SEND, account_id=int(account_id))
        if blocked is not None:
            logger.warning(
                "ai_agent_send_blocked_execution_guard",
                account_id=int(account_id),
                reason=blocked.reason_code,
                path="send_sync",
            )
            return guard_blocked_ai_send(blocked)

        if not _ai_agent_account_gate_passes(account_id):
            logger.warning(
                "ai_agent_telegram_account_forbidden",
                account_id=int(account_id),
                path="send",
            )
            return {
                "ok": False,
                "telegram_message_id": None,
                "error_code": "forbidden_account",
                "error_message": (
                    "This account is not reserved for AI Agent. "
                    "Choose a dedicated AI Agent account."
                ),
            }
        if _gateway_enabled():
            _log_gateway_enabled_once()
            try:
                gw = self._gw()
            except Exception as e:
                logger.warning(
                    "ai_agent_gateway_client_init_failed",
                    error=str(e),
                    path="send",
                )
                return {
                    "ok": False,
                    "telegram_message_id": None,
                    "error_code": "gateway_error",
                    "error_message": "Telegram gateway client unavailable",
                    "transient": True,
                }
            try:
                jid = gw.enqueue_send_message(account_id, target, text)
                logger.info(
                    "ai_agent_gateway_enqueue_send",
                    account_id=int(account_id),
                    job_id=int(jid),
                )
            except (SAOperationalError, sqlite3.OperationalError) as e:
                if _is_sqlite_busy_exception(e):
                    logger.warning(
                        "ai_agent_gateway_enqueue_database_busy",
                        path="send",
                        error=str(e),
                    )
                    return {
                        "ok": False,
                        "telegram_message_id": None,
                        "error_code": "database_busy",
                        "error_message": "SQLite database is busy; retry shortly.",
                        "retryable": True,
                        "transient": True,
                    }
                logger.warning(
                    "ai_agent_gateway_enqueue_failed",
                    error=str(e),
                    path="send",
                )
                return {
                    "ok": False,
                    "telegram_message_id": None,
                    "error_code": "gateway_error",
                    "error_message": "Could not enqueue Telegram gateway job",
                    "transient": True,
                }
            except Exception as e:
                logger.warning(
                    "ai_agent_gateway_enqueue_failed",
                    error=str(e),
                    path="send",
                )
                return {
                    "ok": False,
                    "telegram_message_id": None,
                    "error_code": "gateway_error",
                    "error_message": "Could not enqueue Telegram gateway job",
                    "transient": True,
                }
            return apply_send_wait_result(gw.wait_for_job(jid))
        return _run_async(
            self._get_direct().send_message_async(account_id, target, text)
        )
