"""Owner DM send-intent / delivery records (Wave 6A).

Dedicated table: ``message_deliveries.target_id`` is NOT NULL + FK to chat_targets,
which cannot represent private Telegram peers without polluting scheduler targets.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from src.core.database import Base

# Intent / delivery states (at-most-once retry model)
STATUS_CREATED = "CREATED"
STATUS_SENDING = "SENDING"
STATUS_SENT = "SENT"
STATUS_FAILED = "FAILED"
STATUS_UNCERTAIN = "UNCERTAIN"

OWNER_DM_STATUSES = frozenset(
    {STATUS_CREATED, STATUS_SENDING, STATUS_SENT, STATUS_FAILED, STATUS_UNCERTAIN}
)

PRODUCT_OWNER_DM = "OWNER_DM"


class OwnerDmIntent(Base):
    """Durable owner direct-message send intent + delivery outcome."""

    __tablename__ = "owner_dm_intents"

    id = Column(Integer, primary_key=True, index=True)
    idempotency_key = Column(String(64), nullable=False)
    account_id = Column(Integer, nullable=False, index=True)

    peer_id = Column(String(64), nullable=False)
    peer_type = Column(String(32), nullable=False, default="private")
    peer_username = Column(String(255), nullable=True)

    message_hash = Column(String(64), nullable=False)
    message_preview = Column(String(160), nullable=True)

    status = Column(String(20), nullable=False, default=STATUS_CREATED)
    telegram_message_id = Column(BigInteger, nullable=True)
    error_code = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)

    claim_owner = Column(String(128), nullable=True)
    attempt_started_at = Column(DateTime, nullable=True)
    sent_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    product = Column(String(32), nullable=False, default=PRODUCT_OWNER_DM)

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_owner_dm_intents_idempotency_key"),
        Index("ix_owner_dm_intents_account_status", "account_id", "status"),
    )


class OwnerBulkScheduleIdempotency(Base):
    """Durable ledger for multi-account schedule-bulk requests (Final P1).

    Stores payload fingerprint + last response so lost-HTTP / two-tab replay
    cannot create a second logical batch, and conflicting payloads are rejected.
    """

    __tablename__ = "owner_bulk_schedule_idempotency"

    idempotency_key = Column(String(128), primary_key=True)
    payload_fingerprint = Column(String(64), nullable=False)
    response_json = Column(Text, nullable=False)
    created_count = Column(Integer, nullable=False, default=0)
    failed_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

