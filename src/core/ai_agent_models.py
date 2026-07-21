"""SQLAlchemy models for AI Agent negotiation desk (additive tables)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import relationship

from src.core.database import Base


class AiAgentTask(Base):
    __tablename__ = "ai_agent_tasks"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False, index=True)
    target_username_or_id = Column(String(255), nullable=False)
    goal_text = Column(Text, nullable=False)
    language = Column(String(16), nullable=False, default="auto")
    tone = Column(String(64), nullable=False, default="professional")
    max_messages = Column(Integer, nullable=False, default=10)
    status = Column(String(32), nullable=False, default="draft")
    negotiation_stage = Column(String(64), nullable=False, default="opening")
    auto_mode = Column(String(16), nullable=True, default="autonomous")
    auto_delay_sec = Column(Integer, nullable=True, default=20)
    auto_last_run_at = Column(DateTime, nullable=True)
    final_summary = Column(Text, nullable=True)
    last_activity_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    messages = relationship(
        "AiAgentMessage",
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="AiAgentMessage.id",
    )


class AiAgentMessage(Base):
    __tablename__ = "ai_agent_messages"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("ai_agent_tasks.id"), nullable=False, index=True)
    direction = Column(String(16), nullable=False)
    status = Column(String(32), nullable=False)
    body = Column(Text, nullable=False, default="")
    telegram_message_id = Column(Integer, nullable=True)
    meta_json = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    task = relationship("AiAgentTask", back_populates="messages")


class AiAgentAudit(Base):
    __tablename__ = "ai_agent_audit"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("ai_agent_tasks.id"), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=True, index=True)
    action = Column(String(64), nullable=False)
    detail_json = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
