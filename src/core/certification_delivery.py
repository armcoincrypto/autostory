"""Idempotent MessageDelivery finalization for certified gateway sends.

Ownership: after Telegram confirms a message ID on the gateway rail, persist
exactly one MessageDelivery and mark the linked ScheduledJob SENT.

Critical rule: persistence failure after a confirmed Telegram send must NEVER
trigger another Telegram send — only reconciliation / retry of persistence.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

import structlog
from sqlalchemy.orm import Session

from src.core.scheduler_models import (
    DeliveryStatus,
    JobStatus,
    MessageDelivery,
    ScheduledJob,
)

logger = structlog.get_logger(__name__)

# Jobs with these last_error markers are owned by the telegram-gateway rail.
GATEWAY_OWNED_CERTIFICATION_MARKERS = frozenset(
    {
        "__p5c_certification__",
        "__p5d_certification__",
        "__p6_4_certification__",
    }
)


def gateway_delivery_idempotency_key(gateway_job_id: int) -> str:
    return f"gateway_job:{int(gateway_job_id)}"


def is_gateway_owned_certification_marker(marker: Optional[str]) -> bool:
    return str(marker or "").strip() in GATEWAY_OWNED_CERTIFICATION_MARKERS


def upsert_sent_delivery_for_gateway_cert(
    db: Session,
    *,
    gateway_job_id: int,
    scheduled_job_id: Optional[int],
    account_id: int,
    target_id: int,
    telegram_message_id: int,
    rendered_body: str = "",
    binding_id: Optional[int] = None,
    content_sha256: Optional[str] = None,
) -> dict[str, Any]:
    """
    Persist/upsert one SENT MessageDelivery and mark ScheduledJob SENT.

    Idempotent on ``gateway_job:{id}`` and on ``(job_id, tg_message_id)``.
    ``binding_id`` / ``content_sha256`` are recorded in the audit return value
    (MessageDelivery schema has no dedicated columns for them).
    """
    now = datetime.utcnow()
    key = gateway_delivery_idempotency_key(gateway_job_id)
    tg_mid = int(telegram_message_id)
    body = (rendered_body or "")[:500] or None

    existing = (
        db.query(MessageDelivery)
        .filter(MessageDelivery.idempotency_key == key)
        .first()
    )
    if existing is None and scheduled_job_id is not None:
        existing = (
            db.query(MessageDelivery)
            .filter(
                MessageDelivery.job_id == int(scheduled_job_id),
                MessageDelivery.tg_message_id == tg_mid,
            )
            .order_by(MessageDelivery.id.asc())
            .first()
        )

    created = False
    if existing is None:
        existing = MessageDelivery(
            job_id=int(scheduled_job_id) if scheduled_job_id is not None else None,
            account_id=int(account_id),
            target_id=int(target_id),
            type="PROMO",
            status=DeliveryStatus.SENT.value,
            rendered_body=body,
            tg_message_id=tg_mid,
            sent_at=now,
            attempt_started_at=now,
            idempotency_key=key,
        )
        db.add(existing)
        db.flush()
        created = True
    else:
        # Upgrade / fill without creating a second row.
        if existing.status != DeliveryStatus.SENT.value:
            existing.status = DeliveryStatus.SENT.value
        if existing.tg_message_id is None:
            existing.tg_message_id = tg_mid
        if existing.sent_at is None:
            existing.sent_at = now
        if not existing.rendered_body and body:
            existing.rendered_body = body
        if not existing.idempotency_key:
            existing.idempotency_key = key
        if existing.job_id is None and scheduled_job_id is not None:
            existing.job_id = int(scheduled_job_id)

    job_marked = False
    if scheduled_job_id is not None:
        job = db.query(ScheduledJob).filter(ScheduledJob.id == int(scheduled_job_id)).first()
        if job is not None and str(job.status) != JobStatus.SENT.value:
            job.status = JobStatus.SENT.value
            job.updated_at = now
            job.lease_until = None
            job.lease_owner = None
            job_marked = True

    audit = {
        "delivery_id": int(existing.id),
        "created": created,
        "scheduled_job_marked_sent": job_marked,
        "gateway_job_id": int(gateway_job_id),
        "scheduled_job_id": int(scheduled_job_id) if scheduled_job_id is not None else None,
        "telegram_message_id": tg_mid,
        "binding_id": int(binding_id) if binding_id is not None else None,
        "content_sha256": content_sha256,
        "idempotency_key": key,
    }
    logger.info("gateway_cert_delivery_upserted", **audit)
    return audit
