"""
Dedicated process: drain telegram_gateway_jobs using TelegramDirectTransport.

Run: python -m src.telegram_gateway.worker
"""

from __future__ import annotations

import asyncio
import os
import random
import sys
from datetime import datetime, timedelta
from typing import Any, Optional

import structlog

# Ensure project root on path when invoked as -m
_here = os.path.dirname(os.path.abspath(__file__))
_project_root = os.path.abspath(os.path.join(_here, "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from src.ai_agent.account_allowlist import gateway_claim_allowed_account_ids
from src.ai_agent.telegram_single_sender import TelegramDirectTransport
from src.ai_agent.transient_errors import is_transient_telegram_error
from src.clients.manager import client_manager
from src.core.database import get_db_context
from src.telegram_gateway.service import (
    _running_stale_sec,
    claim_next_jobs,
    mark_job_done,
    mark_job_failed,
    reset_stale_running_jobs,
)

logger = structlog.get_logger(__name__)

_transport = TelegramDirectTransport()


async def _release_gateway_telethon_session(
    account_id: Optional[int], *, job_id: int, operation: str
) -> None:
    """
    Disconnect pooled Telethon client and release per-account fcntl session lock.

    Called after every gateway job so readiness/scheduler are not blocked.
    """
    if account_id is None:
        return
    aid = int(account_id)
    try:
        removed = await client_manager.remove_account(aid)
    except Exception as e:
        logger.warning(
            "telegram_gateway_account_release_failed",
            account_id=aid,
            job_id=int(job_id),
            operation=str(operation),
            error=str(e),
        )
        return
    if removed:
        logger.info(
            "telegram_gateway_account_released",
            account_id=aid,
            job_id=int(job_id),
            operation=str(operation),
        )


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return float(default)
    try:
        return float(str(raw).strip())
    except ValueError:
        return float(default)


def _dedupe_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[int] = set()
    out: list[dict[str, Any]] = []
    for m in messages:
        tid = m.get("telegram_message_id")
        if tid is None:
            continue
        try:
            k = int(tid)
        except (TypeError, ValueError):
            continue
        if k in seen:
            continue
        seen.add(k)
        out.append(m)
    return out


async def _run_send(
    job_id: int,
    account_id: int,
    target: str,
    payload: dict,
    attempts_before: int,
) -> None:
    try:
        text = str((payload or {}).get("text") or "")
        res = await _transport.send_message_async(account_id, target, text)
        with get_db_context() as db:
            if res.get("ok"):
                mark_job_done(
                    db,
                    job_id,
                    {
                        "ok": True,
                        "telegram_message_id": res.get("telegram_message_id"),
                    },
                )
                logger.info(
                    "telegram_gateway_job_done",
                    job_id=job_id,
                    account_id=account_id,
                    task_type="send_message",
                )
                return
            code = str(res.get("error_code") or "send_failed")
            msg = str(res.get("error_message") or "")
            transient = is_transient_telegram_error(code, msg)
            if transient:
                exp = min(5, max(0, int(attempts_before)))
                backoff = min(120.0, (2**exp) + random.uniform(0, 1.5))
                mark_job_failed(
                    db,
                    job_id,
                    error_code=code,
                    error_message=msg[:2000],
                    retry_at=datetime.utcnow() + timedelta(seconds=backoff),
                    increment_attempts=True,
                )
                logger.warning(
                    "telegram_gateway_job_retry",
                    job_id=job_id,
                    account_id=account_id,
                    error_code=code,
                )
            else:
                mark_job_failed(
                    db,
                    job_id,
                    error_code=code,
                    error_message=msg[:2000],
                    retry_at=None,
                    increment_attempts=True,
                )
                logger.warning(
                    "telegram_gateway_job_failed",
                    job_id=job_id,
                    account_id=account_id,
                    error_code=code,
                )
    finally:
        await _release_gateway_telethon_session(
            account_id, job_id=int(job_id), operation="send_message"
        )


async def _run_fetch(
    job_id: int,
    account_id: int,
    target: str,
    payload: dict,
    attempts_before: int,
) -> None:
    try:
        lim = int((payload or {}).get("limit") or 20)
        res = await _transport.fetch_recent_messages_async(account_id, target, lim)
        with get_db_context() as db:
            if res.get("ok"):
                msgs = _dedupe_messages(list(res.get("messages") or []))
                mark_job_done(db, job_id, {"ok": True, "messages": msgs})
                logger.info(
                    "telegram_gateway_job_done",
                    job_id=job_id,
                    account_id=account_id,
                    task_type="fetch_messages",
                    message_count=len(msgs),
                )
                return
            code = str(res.get("error_code") or "fetch_failed")
            msg = str(res.get("error_message") or "")
            transient = is_transient_telegram_error(code, msg)
            if transient:
                exp = min(5, max(0, int(attempts_before)))
                backoff = min(120.0, (2**exp) + random.uniform(0, 1.5))
                mark_job_failed(
                    db,
                    job_id,
                    error_code=code,
                    error_message=msg[:2000],
                    retry_at=datetime.utcnow() + timedelta(seconds=backoff),
                    increment_attempts=True,
                )
                logger.warning(
                    "telegram_gateway_job_retry",
                    job_id=job_id,
                    account_id=account_id,
                    error_code=code,
                )
            else:
                mark_job_failed(
                    db,
                    job_id,
                    error_code=code,
                    error_message=msg[:2000],
                    retry_at=None,
                    increment_attempts=True,
                )
                logger.warning(
                    "telegram_gateway_job_failed",
                    job_id=job_id,
                    account_id=account_id,
                    error_code=code,
                )
    finally:
        await _release_gateway_telethon_session(
            account_id, job_id=int(job_id), operation="fetch_messages"
        )


async def _process_job_snapshot(snap: tuple) -> None:
    jid, aid, ttype, target, payload, att_before = snap
    ttype = (ttype or "").strip().lower()
    target = (target or "").strip()
    try:
        with get_db_context() as db:
            allowed_ids = gateway_claim_allowed_account_ids(db)
        if allowed_ids is not None and int(aid) not in allowed_ids:
            with get_db_context() as db:
                mark_job_failed(
                    db,
                    int(jid),
                    error_code="account_not_allowed_for_ai_agent",
                    error_message="Account is not in the AI Agent gateway allowlist.",
                    retry_at=None,
                    increment_attempts=False,
                )
            logger.warning(
                "telegram_gateway_job_rejected_account_not_ai_allowed",
                job_id=int(jid),
                account_id=int(aid),
            )
            return
    except Exception as e:
        logger.warning("telegram_gateway_ai_allowlist_check_failed", error=str(e))
    try:
        if ttype == "send_message":
            await _run_send(jid, aid, target, payload, att_before)
        elif ttype == "fetch_messages":
            await _run_fetch(jid, aid, target, payload, att_before)
        else:
            with get_db_context() as db:
                mark_job_failed(
                    db,
                    jid,
                    error_code="unknown_task_type",
                    error_message=ttype,
                    retry_at=None,
                    increment_attempts=False,
                )
            logger.warning(
                "telegram_gateway_unknown_task",
                job_id=jid,
                task_type=ttype,
            )
    except Exception as e:
        logger.exception(
            "telegram_gateway_job_exception",
            job_id=jid,
            account_id=aid,
            error=str(e),
        )
        with get_db_context() as db:
            mark_job_failed(
                db,
                jid,
                error_code="gateway_exception",
                error_message=f"{type(e).__name__}: {e}"[:2000],
                retry_at=datetime.utcnow() + timedelta(seconds=30),
                increment_attempts=True,
            )


def _recover_stale() -> None:
    try:
        sec = _running_stale_sec()
        with get_db_context() as db:
            n = reset_stale_running_jobs(db, older_than_sec=sec)
        if n:
            logger.warning(
                "telegram_gateway_recovered_stale_running",
                count=n,
                stale_sec=sec,
            )
    except Exception as e:
        logger.warning("telegram_gateway_recover_failed", error=str(e))


async def worker_loop() -> None:
    poll = _env_float("TELEGRAM_GATEWAY_POLL_SEC", 2.0)
    poll = max(0.5, min(poll, 10.0))
    logger.info("telegram_gateway_worker_start", poll_sec=poll)
    _recover_stale()
    cycle = 0
    while True:
        cycle += 1
        if cycle % 30 == 1:
            _recover_stale()
        claimed: list = []
        try:
            with get_db_context() as db:
                allowed = gateway_claim_allowed_account_ids(db)
                claimed = claim_next_jobs(
                    db, limit=3, allowed_account_ids=allowed
                )
        except Exception as e:
            logger.warning("telegram_gateway_claim_failed", error=str(e))
            await asyncio.sleep(poll)
            continue
        if not claimed:
            await asyncio.sleep(poll)
            continue
        snapshots = [
            (
                int(j.id),
                int(j.account_id),
                j.task_type,
                j.target,
                dict(j.payload_json or {}),
                int(j.attempts or 0),
            )
            for j in claimed
        ]
        for snap in snapshots:
            await _process_job_snapshot(snap)


def main() -> None:
    try:
        asyncio.run(worker_loop())
    except KeyboardInterrupt:
        logger.info("telegram_gateway_worker_stopped")


if __name__ == "__main__":
    main()
