"""SQLAlchemy model for DB-backed Telegram gateway job queue."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, JSON, String, Text

from src.core.database import Base


class TelegramGatewayJob(Base):
    """
    One unit of work: send or fetch for a single account.
    The gateway worker serializes execution per account_id.
    """

    __tablename__ = "telegram_gateway_jobs"

    id = Column(Integer, primary_key=True, autoincrement=True, index=True)
    account_id = Column(Integer, nullable=False, index=True)
    task_type = Column(String(32), nullable=False, index=True)
    target = Column(String(512), nullable=False, default="")
    payload_json = Column(JSON, nullable=True)
    status = Column(String(16), nullable=False, default="pending", index=True)
    result_json = Column(JSON, nullable=True)
    error_code = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    run_after = Column(DateTime, nullable=True, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )
