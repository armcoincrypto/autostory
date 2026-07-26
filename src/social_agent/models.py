"""Social Agent persistence models (additive tables on shared SQLite)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, UniqueConstraint

from src.core.database import Base


class SocialAgentConversation(Base):
    __tablename__ = "social_agent_conversations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(255), nullable=False, default="New chat")
    created_by = Column(String(128), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class SocialAgentMessage(Base):
    __tablename__ = "social_agent_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(Integer, nullable=False, index=True)
    role = Column(String(32), nullable=False)  # user | assistant | system | tool
    content = Column(Text, nullable=False, default="")
    tool_name = Column(String(128), nullable=True)
    tool_payload_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class SocialContentItem(Base):
    __tablename__ = "social_content_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(255), nullable=False, default="")
    status = Column(String(64), nullable=False, default="DRAFT", index=True)
    brief = Column(Text, nullable=True)
    created_by = Column(String(128), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class SocialContentVariant(Base):
    __tablename__ = "social_content_variants"
    __table_args__ = (
        UniqueConstraint("content_id", "platform", "language", name="uq_social_variant_plat_lang"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    content_id = Column(Integer, nullable=False, index=True)
    platform = Column(String(64), nullable=False)
    language = Column(String(16), nullable=False, default="EN")
    body = Column(Text, nullable=False, default="")
    char_count = Column(Integer, nullable=False, default=0)
    validation_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class SocialConnection(Base):
    __tablename__ = "social_connections"

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider = Column(String(64), nullable=False, index=True)  # meta | telegram | x | ...
    display_name = Column(String(255), nullable=False, default="")
    external_account_id = Column(String(128), nullable=True)
    status = Column(String(64), nullable=False, default="not_configured")
    health = Column(String(64), nullable=False, default="unknown")
    permissions_json = Column(Text, nullable=True)
    token_expires_at = Column(DateTime, nullable=True)
    last_checked_at = Column(DateTime, nullable=True)
    # Encrypted JSON envelope — never plaintext tokens.
    credentials_encrypted = Column(Text, nullable=True)
    destinations_json = Column(Text, nullable=True)
    selected_page_id = Column(String(128), nullable=True)
    selected_instagram_id = Column(String(128), nullable=True)
    workspace_id = Column(String(64), nullable=False, default="default", index=True)
    created_by = Column(String(128), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class SocialOAuthState(Base):
    __tablename__ = "social_oauth_states"

    id = Column(Integer, primary_key=True, autoincrement=True)
    state = Column(String(128), nullable=False, unique=True, index=True)
    provider = Column(String(64), nullable=False, index=True)
    actor = Column(String(128), nullable=False)
    workspace_id = Column(String(64), nullable=False, default="default")
    redirect_uri = Column(String(512), nullable=False)
    purpose = Column(String(64), nullable=False, default="connect")  # connect | reconnect
    connection_id = Column(Integer, nullable=True)
    used_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class SocialAgentAuditEvent(Base):
    __tablename__ = "social_agent_audit_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    actor = Column(String(128), nullable=True)
    action = Column(String(128), nullable=False, index=True)
    tool_name = Column(String(128), nullable=True)
    decision = Column(String(64), nullable=True)
    dry_run = Column(Boolean, nullable=False, default=False)
    detail_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class SocialIdempotencyRecord(Base):
    __tablename__ = "social_idempotency_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    idempotency_key = Column(String(128), nullable=False, unique=True, index=True)
    action = Column(String(128), nullable=False)
    result_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class SocialPublishDryRun(Base):
    """Persisted publishing dry-run previews — never a live publish receipt."""

    __tablename__ = "social_publish_dry_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    workspace_id = Column(String(64), nullable=False, default="default", index=True)
    actor = Column(String(128), nullable=True)
    content_id = Column(Integer, nullable=True, index=True)
    destinations_json = Column(Text, nullable=False, default="[]")
    content_snapshot_json = Column(Text, nullable=True)
    validation_json = Column(Text, nullable=True)
    payloads_json = Column(Text, nullable=True)
    warnings_json = Column(Text, nullable=True)
    payload_hash = Column(String(64), nullable=False, index=True)
    status = Column(String(64), nullable=False, default="READY", index=True)
    provider_called = Column(Boolean, nullable=False, default=False)
    provider_http_posts = Column(Integer, nullable=False, default=0)
    idempotency_key = Column(String(128), nullable=True, unique=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class SocialPublishCanaryAuthorization(Base):
    """Short-lived single-use canary authorization — never a caller boolean."""

    __tablename__ = "social_publish_canary_authorizations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    actor = Column(String(128), nullable=False)
    workspace_id = Column(String(64), nullable=False, default="default", index=True)
    page_id = Column(String(128), nullable=False, index=True)
    dry_run_id = Column(Integer, nullable=False, index=True)
    payload_hash = Column(String(64), nullable=False, index=True)
    idempotency_key = Column(String(128), nullable=False, unique=True, index=True)
    destination = Column(String(64), nullable=False, default="facebook_page")
    execution_mode = Column(String(64), nullable=False, default="controlled-canary")
    secret_hash = Column(String(64), nullable=False)
    status = Column(String(32), nullable=False, default="ACTIVE", index=True)
    explicit_approval = Column(String(32), nullable=False, default="")
    expires_at = Column(DateTime, nullable=False, index=True)
    used_at = Column(DateTime, nullable=True)
    result_status = Column(String(64), nullable=True)
    external_post_id = Column(String(128), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class SocialPublishReceipt(Base):
    """Reconciled provider publish receipt (canary / future live)."""

    __tablename__ = "social_publish_receipts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    workspace_id = Column(String(64), nullable=False, default="default", index=True)
    actor = Column(String(128), nullable=True)
    provider = Column(String(64), nullable=False, default="meta")
    destination = Column(String(64), nullable=False)
    page_id = Column(String(128), nullable=False, index=True)
    dry_run_id = Column(Integer, nullable=True, index=True)
    authorization_id = Column(Integer, nullable=True, index=True)
    payload_hash = Column(String(64), nullable=False, index=True)
    idempotency_key = Column(String(128), nullable=False, unique=True, index=True)
    external_post_id = Column(String(128), nullable=True, index=True)
    provider_request_id = Column(String(128), nullable=True)
    provider_response_category = Column(String(64), nullable=True)
    http_status = Column(Integer, nullable=True)
    execution_mode = Column(String(64), nullable=False)
    status = Column(String(64), nullable=False, default="SUCCEEDED", index=True)
    public_url = Column(String(512), nullable=True)
    detail_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
