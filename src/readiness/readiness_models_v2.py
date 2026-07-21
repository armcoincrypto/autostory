"""
P9.5 — Additive readiness v2 table (separate from legacy account_readiness_snapshots).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint

from src.core.database import Base


class AccountReadinessSnapshotV2(Base):
    """V2 readiness truth — written only by readiness_store_v2 / worker_v2."""

    __tablename__ = "account_readiness_snapshots_v2"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False, unique=True, index=True)
    status = Column(String(32), nullable=False)
    reason = Column(Text, nullable=True)
    failure_code = Column(String(64), nullable=True)
    checked_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (UniqueConstraint("account_id", name="uq_readiness_v2_account_id"),)
