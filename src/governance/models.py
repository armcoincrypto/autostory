"""P10.21 additive governance tables."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint

from src.core.database import Base


class AccountRuntimeRole(Base):
    __tablename__ = "account_runtime_roles"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False, index=True)
    role = Column(String(64), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_by = Column(String(128), nullable=True)
    reason = Column(Text, nullable=True)
    active = Column(Boolean, default=True, nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint("account_id", "role", name="uq_account_runtime_role"),
        Index("ix_account_runtime_roles_account_active", "account_id", "active"),
    )


class AccountRuntimeTag(Base):
    __tablename__ = "account_runtime_tags"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False, index=True)
    tag = Column(String(64), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_by = Column(String(128), nullable=True)
    active = Column(Boolean, default=True, nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint("account_id", "tag", name="uq_account_runtime_tag"),
        Index("ix_account_runtime_tags_account_active", "account_id", "active"),
    )


class AccountGovernanceAuditLog(Base):
    __tablename__ = "account_governance_audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=True, index=True)
    action = Column(String(64), nullable=False, index=True)
    target_type = Column(String(32), nullable=False)
    target_value = Column(String(128), nullable=False)
    reason = Column(Text, nullable=True)
    actor = Column(String(128), nullable=True)
    payload_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class AccountPinned(Base):
    __tablename__ = "account_pinned"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False, unique=True, index=True)
    pinned_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    pinned_by = Column(String(128), nullable=True)
    note = Column(Text, nullable=True)
    active = Column(Boolean, default=True, nullable=False, index=True)


class AccountCohort(Base):
    __tablename__ = "account_cohorts"

    id = Column(Integer, primary_key=True, index=True)
    slug = Column(String(64), nullable=False, unique=True, index=True)
    name = Column(String(128), nullable=False)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_by = Column(String(128), nullable=True)
    active = Column(Boolean, default=True, nullable=False, index=True)


class AccountCohortMember(Base):
    __tablename__ = "account_cohort_members"

    id = Column(Integer, primary_key=True, index=True)
    cohort_id = Column(Integer, ForeignKey("account_cohorts.id"), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False, index=True)
    added_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    added_by = Column(String(128), nullable=True)
    active = Column(Boolean, default=True, nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint("cohort_id", "account_id", name="uq_account_cohort_member"),
        Index("ix_account_cohort_members_cohort_active", "cohort_id", "active"),
    )
