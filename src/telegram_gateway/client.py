"""Client for web workers: enqueue jobs and poll until done (AI Agent)."""

from __future__ import annotations

import os
import time
from typing import Any, Optional

import structlog

from src.telegram_gateway import errors as gw_errors
from src.telegram_gateway.models import TelegramGatewayJob
from src.telegram_gateway.service import enqueue_job, get_job

logger = structlog.get_logger(__name__)


def gateway_client_wait_sec() -> float:
    raw = os.environ.get("TELEGRAM_GATEWAY_CLIENT_WAIT_SEC", "120").strip()
    try:
        return max(20.0, min(float(raw), 300.0))
    except ValueError:
        return 120.0


def apply_send_wait_result(out: dict[str, Any]) -> dict[str, Any]:
    """Normalize wait_for_job payload to TelegramSingleSender.send_message shape."""
    if out.get("error_code") == gw_errors.GATEWAY_TIMEOUT:
        return {
            "ok": False,
            "telegram_message_id": None,
            "error_code": gw_errors.GATEWAY_TIMEOUT,
            "error_message": out.get("error_message") or "Gateway timeout",
            "transient": True,
        }
    if not out.get("ok"):
        return {
            "ok": False,
            "telegram_message_id": None,
            "error_code": str(out.get("error_code") or "gateway_error"),
            "error_message": str(out.get("error_message") or "Gateway error"),
            "transient": bool(out.get("transient")),
        }
    return {
        "ok": True,
        "telegram_message_id": out.get("telegram_message_id"),
        "error_code": None,
        "error_message": None,
    }


def apply_fetch_wait_result(out: dict[str, Any]) -> dict[str, Any]:
    """Normalize wait_for_job payload to TelegramSingleSender.fetch_recent_messages shape."""
    if out.get("error_code") == gw_errors.GATEWAY_TIMEOUT:
        return {
            "ok": False,
            "messages": [],
            "error_code": gw_errors.GATEWAY_TIMEOUT,
            "error_message": out.get("error_message") or "Gateway timeout",
            "transient": True,
        }
    if not out.get("ok"):
        return {
            "ok": False,
            "messages": out.get("messages") or [],
            "error_code": str(out.get("error_code") or "gateway_error"),
            "error_message": str(out.get("error_message") or "Gateway error"),
            "transient": bool(out.get("transient")),
        }
    return {
        "ok": True,
        "messages": list(out.get("messages") or []),
    }


def _use_gateway() -> bool:
    raw = os.environ.get("AI_AGENT_USE_TELEGRAM_GATEWAY", "true").strip().lower()
    return raw in ("1", "true", "yes", "on")


class TelegramGatewayClient:
    """Enqueue + wait; returns same shape as TelegramSingleSender sync methods."""

    def enqueue_send_message(self, account_id: int, target: str, text: str) -> int:
        return enqueue_job(
            account_id=account_id,
            task_type="send_message",
            target=(target or "").strip(),
            payload={"text": str(text or "")},
        )

    def enqueue_fetch_messages(
        self, account_id: int, target: str, limit: int = 20
    ) -> int:
        return enqueue_job(
            account_id=account_id,
            task_type="fetch_messages",
            target=(target or "").strip(),
            payload={"limit": int(limit)},
        )

    def get_job(self, job_id: int) -> Optional[TelegramGatewayJob]:
        return get_job(job_id)

    def wait_for_job(
        self,
        job_id: int,
        *,
        timeout_sec: Optional[float] = None,
        poll_sec: float = 0.25,
    ) -> dict[str, Any]:
        to = float(timeout_sec) if timeout_sec is not None else gateway_client_wait_sec()
        deadline = time.monotonic() + to
        while time.monotonic() < deadline:
            row = get_job(job_id)
            if not row:
                return {
                    "ok": False,
                    "error_code": gw_errors.GATEWAY_JOB_NOT_FOUND,
                    "error_message": "Gateway job not found",
                }
            st = (row.status or "").strip().lower()
            if st == "done":
                res = row.result_json if isinstance(row.result_json, dict) else {}
                return dict(res)
            if st == "failed":
                return {
                    "ok": False,
                    "error_code": row.error_code or "gateway_failed",
                    "error_message": row.error_message or "Gateway job failed",
                }
            # pending / running / retry — keep polling until terminal or timeout
            time.sleep(float(poll_sec))
        logger.warning(
            "ai_agent_gateway_timeout",
            job_id=int(job_id),
            wait_event="telegram_gateway_wait_timeout",
        )
        return {
            "ok": False,
            "error_code": gw_errors.GATEWAY_TIMEOUT,
            "error_message": "Gateway job timed out waiting for worker",
            "transient": True,
        }

    def send_message(self, account_id: int, target: str, text: str) -> dict[str, Any]:
        jid = self.enqueue_send_message(account_id, target, text)
        return apply_send_wait_result(self.wait_for_job(jid))

    def fetch_recent_messages(
        self, account_id: int, target: str, limit: int = 20
    ) -> dict[str, Any]:
        jid = self.enqueue_fetch_messages(account_id, target, limit)
        return apply_fetch_wait_result(self.wait_for_job(jid))


def gateway_enabled() -> bool:
    return _use_gateway()
