"""P5D gateway send reconciliation for scoped certification jobs."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import structlog
from sqlalchemy.orm import Session

from src.core.scheduler_models import MessageDelivery, ScheduledJob
from src.telegram_gateway.models import TelegramGatewayJob
from src.telegram_gateway.service import get_job

logger = structlog.get_logger(__name__)

NOT_SENT_CONFIRMED = "NOT_SENT_CONFIRMED"
SENT_CONFIRMED = "SENT_CONFIRMED"
AMBIGUOUS_RECONCILIATION_REQUIRED = "AMBIGUOUS_RECONCILIATION_REQUIRED"
RETRYABLE_PRE_SEND_FAILURE = "RETRYABLE_PRE_SEND_FAILURE"
TERMINAL_POLICY_DENIAL = "TERMINAL_POLICY_DENIAL"


@dataclass
class CertificationSendGuard:
    outcome: str
    tg_message_id: Optional[int] = None
    reason_code: str = ""
    audit: dict[str, Any] = None

    def __post_init__(self) -> None:
        if self.audit is None:
            self.audit = {}


def _delivery_sent_row(db: Session, delivery_id: Optional[int]) -> Optional[MessageDelivery]:
    if not delivery_id:
        return None
    row = db.query(MessageDelivery).filter(MessageDelivery.id == int(delivery_id)).first()
    if row and str(row.status or "").strip().upper() == "SENT" and row.tg_message_id is not None:
        return row
    return None


def _gateway_done_tg_id(gateway_job_id: int) -> Optional[int]:
    row = get_job(int(gateway_job_id))
    if not row or str(row.status or "").strip().lower() != "done":
        return None
    res = row.result_json if isinstance(row.result_json, dict) else {}
    tid = res.get("telegram_message_id")
    if tid is None:
        return None
    try:
        return int(tid)
    except (TypeError, ValueError):
        return None


async def _lookup_message_by_body(account_id: int, target: str, body: str) -> Optional[int]:
    """Telegram-side lookup: prove whether an exact body was already sent."""
    text = (body or "").strip()
    if not text:
        return None
    try:
        from src.ai_agent.telegram_single_sender import TelegramDirectTransport

        transport = TelegramDirectTransport()
        res = await transport.fetch_recent_messages_async(int(account_id), target, 30)
        if not res.get("ok"):
            return None
        for msg in list(res.get("messages") or []):
            if str(msg.get("text") or "").strip() == text:
                tid = msg.get("telegram_message_id")
                if tid is not None:
                    return int(tid)
    except Exception as exc:
        logger.warning(
            "p5d_telegram_lookup_failed",
            account_id=int(account_id),
            error=str(exc)[:200],
        )
    return None


def classify_certification_payload(payload: dict[str, Any]) -> bool:
    return bool(
        payload.get("p5d_certification")
        or payload.get("p5c_certification")
        or payload.get("p6_4_certification")
    )


async def pre_send_certification_guard(
    *,
    gateway_job_id: int,
    account_id: int,
    target: str,
    payload: dict[str, Any],
    db: Optional[Session] = None,
) -> CertificationSendGuard:
    """
    Pre-send guard for certification gateway jobs.

    Prevents duplicate Telegram sends when durable or Telegram-side evidence proves SENT.
    """
    audit: dict[str, Any] = {
        "gateway_job_id": int(gateway_job_id),
        "account_id": int(account_id),
        "scheduled_job_id": payload.get("scheduled_job_id"),
        "delivery_id": payload.get("delivery_id"),
    }
    if not classify_certification_payload(payload):
        return CertificationSendGuard(
            outcome=NOT_SENT_CONFIRMED,
            reason_code="not_certification_payload",
            audit=audit,
        )

    delivery_id = payload.get("delivery_id")
    if db is not None:
        sent = _delivery_sent_row(db, int(delivery_id) if delivery_id else None)
        if sent is not None:
            audit["delivery_id"] = int(sent.id)
            return CertificationSendGuard(
                outcome=SENT_CONFIRMED,
                tg_message_id=int(sent.tg_message_id),
                reason_code="delivery_already_sent",
                audit=audit,
            )

    existing_tid = _gateway_done_tg_id(int(gateway_job_id))
    if existing_tid is not None:
        audit["gateway_result_tg_message_id"] = existing_tid
        return CertificationSendGuard(
            outcome=SENT_CONFIRMED,
            tg_message_id=existing_tid,
            reason_code="gateway_already_done",
            audit=audit,
        )

    body = str(payload.get("text") or "")
    looked_up = await _lookup_message_by_body(int(account_id), target, body)
    if looked_up is not None:
        audit["telegram_lookup_tg_message_id"] = looked_up
        logger.info(
            "p5d_send_reconciled_from_telegram_lookup",
            gateway_job_id=int(gateway_job_id),
            account_id=int(account_id),
            tg_message_id=looked_up,
        )
        return CertificationSendGuard(
            outcome=SENT_CONFIRMED,
            tg_message_id=looked_up,
            reason_code="telegram_lookup_confirmed_sent",
            audit=audit,
        )

    # Post-send ambiguity without Telegram proof: block automatic resend in scenario C recovery.
    from src.core.p5d_failpoints import p5d_certification_mode

    if p5d_certification_mode() and str(payload.get("p5d_scenario") or "") == "C":
        gw = get_job(int(gateway_job_id))
        if gw and str(gw.status or "").lower() in ("running", "retry") and int(gw.attempts or 0) > 0:
            return CertificationSendGuard(
                outcome=AMBIGUOUS_RECONCILIATION_REQUIRED,
                reason_code="post_send_ambiguous_no_telegram_proof",
                audit=audit,
            )

    return CertificationSendGuard(
        outcome=NOT_SENT_CONFIRMED,
        reason_code="safe_to_send",
        audit=audit,
    )


def reconcile_scheduled_job_terminal(
    db: Session,
    *,
    scheduled_job_id: int,
    delivery_id: Optional[int],
    tg_message_id: int,
) -> dict[str, Any]:
    """Idempotent check that scheduled job and delivery are terminal SENT."""
    job = db.query(ScheduledJob).filter(ScheduledJob.id == int(scheduled_job_id)).first()
    delivery = (
        db.query(MessageDelivery).filter(MessageDelivery.id == int(delivery_id)).first()
        if delivery_id
        else None
    )
    return {
        "job_status": str(job.status) if job else None,
        "delivery_status": str(delivery.status) if delivery else None,
        "delivery_tg_message_id": int(delivery.tg_message_id) if delivery and delivery.tg_message_id else None,
        "expected_tg_message_id": int(tg_message_id),
    }
