"""
Auto Message Scheduler Models
Target chats, templates, schedule rules, jobs, delivery logs
"""
from datetime import datetime
from enum import Enum

from sqlalchemy import (
    Column, Integer, String, Text, Boolean, DateTime,
    ForeignKey, JSON, Enum as SQLEnum, BigInteger, UniqueConstraint
)
from sqlalchemy.orm import relationship, backref

from .database import Base


class MessageType(str, Enum):
    PROMO = "PROMO"
    INFO = "INFO"


class TemplateScope(str, Enum):
    GLOBAL = "GLOBAL"
    ACCOUNT = "ACCOUNT"
    TARGET = "TARGET"
    BINDING = "BINDING"


class TargetMode(str, Enum):
    ALL_BOUND = "ALL_BOUND"
    ONLY_SELECTED = "ONLY_SELECTED"


class JobStatus(str, Enum):
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"


class DeliveryStatus(str, Enum):
    SENT = "SENT"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class ChatTarget(Base):
    """Chats/channels where messages will be posted"""
    __tablename__ = "chat_targets"

    id = Column(Integer, primary_key=True, index=True)
    tg_id = Column(BigInteger, nullable=True, index=True)
    username = Column(String(255), nullable=True)
    invite_link = Column(Text, nullable=True)
    title = Column(String(255), nullable=True)
    chat_type = Column(String(50), nullable=False)  # channel, group, supergroup

    is_verified = Column(Boolean, default=False)
    verified_at = Column(DateTime, nullable=True)
    last_check_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    bindings = relationship("AccountTargetBinding", back_populates="target", cascade="all, delete-orphan")


class AccountTargetBinding(Base):
    """Many-to-many: account <-> target, with per-binding settings"""
    __tablename__ = "account_target_bindings"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    target_id = Column(Integer, ForeignKey("chat_targets.id"), nullable=False)

    can_post = Column(Boolean, default=True)
    allowed_types = Column(String(100), default="PROMO,INFO")  # CSV
    daily_cap = Column(Integer, nullable=True)  # override

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (UniqueConstraint("account_id", "target_id", name="uq_account_target"),)

    account = relationship("Account", backref="scheduler_bindings")
    target = relationship("ChatTarget", back_populates="bindings")


class MessageTemplate(Base):
    """Templates for PROMO and INFO message types"""
    __tablename__ = "message_templates"

    id = Column(Integer, primary_key=True, index=True)
    type = Column(String(20), nullable=False)  # PROMO, INFO
    scope = Column(String(20), nullable=False)  # GLOBAL, ACCOUNT, TARGET, BINDING
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=True)
    target_id = Column(Integer, ForeignKey("chat_targets.id"), nullable=True)
    binding_id = Column(Integer, ForeignKey("account_target_bindings.id"), nullable=True)

    name = Column(String(255), nullable=False)
    body = Column(Text, nullable=False)
    is_active = Column(Boolean, default=True)
    weight = Column(Integer, default=100)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ScheduleProfile(Base):
    """Per-account automation settings"""
    __tablename__ = "schedule_profiles"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False, unique=True)

    is_enabled = Column(Boolean, default=False)
    timezone = Column(String(50), default="Asia/Yerevan")
    min_interval_sec = Column(Integer, default=900)
    daily_cap_total = Column(Integer, default=6)
    daily_cap_promo = Column(Integer, default=3)
    daily_cap_info = Column(Integer, default=3)
    jitter_sec = Column(Integer, default=300)
    quiet_hours_json = Column(Text, nullable=True)  # {"start":"00:00","end":"08:00"}

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    account = relationship("Account", backref=backref("schedule_profile", uselist=False))


class ScheduleRule(Base):
    """Times and routing per account + message type"""
    __tablename__ = "schedule_rules"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    type = Column(String(20), nullable=False)  # PROMO, INFO

    times_json = Column(Text, nullable=False)  # ["10:00","18:00"]
    target_mode = Column(String(20), default="ALL_BOUND")  # ALL_BOUND, ONLY_SELECTED
    selected_target_ids_json = Column(Text, nullable=True)  # [1,2,3]
    is_enabled = Column(Boolean, default=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    account = relationship("Account", backref="schedule_rules")


class ScheduledJob(Base):
    """Concrete jobs created by scheduler"""
    __tablename__ = "scheduled_jobs"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    target_id = Column(Integer, ForeignKey("chat_targets.id"), nullable=False)
    type = Column(String(20), nullable=False)
    run_at = Column(DateTime, nullable=False)

    status = Column(String(20), default="PENDING")
    template_id = Column(Integer, ForeignKey("message_templates.id"), nullable=True)
    attempts = Column(Integer, default=0)
    last_error = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    account = relationship("Account", backref="scheduled_jobs")
    target = relationship("ChatTarget", backref="scheduled_jobs")


class MessageDelivery(Base):
    """Audit log of send attempts"""
    __tablename__ = "message_deliveries"

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(Integer, ForeignKey("scheduled_jobs.id"), nullable=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    target_id = Column(Integer, ForeignKey("chat_targets.id"), nullable=False)
    type = Column(String(20), nullable=True)
    template_id = Column(Integer, ForeignKey("message_templates.id"), nullable=True)
    rendered_body = Column(Text, nullable=True)

    sent_at = Column(DateTime, nullable=True)
    status = Column(String(20), nullable=False)
    tg_message_id = Column(BigInteger, nullable=True)
    error_code = Column(String(50), nullable=True)
    error_message = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    account = relationship("Account", backref="message_deliveries")
    target = relationship("ChatTarget", backref="message_deliveries")
