"""
Database Models for STORYFLEET
"""
from datetime import datetime
from enum import Enum
from typing import Optional, List

from sqlalchemy import (
    Column, Integer, String, Text, Boolean, DateTime,
    ForeignKey, JSON, Enum as SQLEnum, Float
)
from sqlalchemy.orm import relationship
from .database import Base


class AccountStatus(str, Enum):
    """Account status enumeration"""
    ACTIVE = "active"
    INACTIVE = "inactive"
    BANNED = "banned"
    FLOOD_WAIT = "flood_wait"
    AUTH_REQUIRED = "auth_required"


class AccountPurpose(str, Enum):
    """Account purpose: autostory for stories, messaging for scheduled group messages"""
    AUTOSTORY = "autostory"
    MESSAGING = "messaging"
    BOTH = "both"


class TaskStatus(str, Enum):
    """Task status enumeration"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskType(str, Enum):
    """Task type enumeration"""
    PUBLISH_STORY = "publish_story"
    DISCOVER_USERS = "discover_users"
    SEND_MESSAGE = "send_message"
    JOIN_CHAT = "join_chat"


class Account(Base):
    """Telegram user account model"""
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, index=True)
    phone_number = Column(String(20), unique=True, nullable=False, index=True)
    session_string = Column(Text, nullable=True)  # Legacy/transient: import input only; not source of truth
    session_path = Column(String(512), nullable=True, index=True)  # Canonical: /opt/autostory/data/sessions/account_<id>.session

    # Account info
    user_id = Column(Integer, nullable=True, index=True)  # Telegram user ID
    username = Column(String(100), nullable=True)
    first_name = Column(String(100), nullable=True)
    last_name = Column(String(100), nullable=True)

    # Status and health — SQLite stores lowercase strings (active, inactive, ...). Use non-native
    # Enum so ORM loads by value, not Python enum member names (avoids LookupError on 'active').
    status = Column(
        SQLEnum(
            AccountStatus,
            native_enum=False,
            length=32,
            values_callable=lambda x: [e.value for e in x],
        ),
        default=AccountStatus.AUTH_REQUIRED,
    )
    last_active = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    flood_wait_until = Column(DateTime, nullable=True)
    story_blocked_until = Column(DateTime, nullable=True)  # Temporary block from STORIES_TOO_MUCH / STORY_SEND_FLOOD; expires

    # Story-specific state (separate from general health; healthy != story-capable)
    story_status = Column(String(20), nullable=True)       # unknown | ok | frozen | rate_limited | restricted
    story_status_reason = Column(String(255), nullable=True)
    story_status_checked_at = Column(DateTime, nullable=True)

    # Last healthcheck result (does not replace operational status)
    health_status = Column(String(20), nullable=True)          # alive/auth_required/frozen/banned/deleted/restricted/error
    health_reason = Column(String(255), nullable=True)         # machine-friendly reason code
    health_message = Column(Text, nullable=True)               # operator-friendly message
    health_checked_at = Column(DateTime, nullable=True)        # UTC timestamp

    # Last live identity audit (DB vs get_me); set on health check; operator hints only — no auto-actions
    identity_audit_status = Column(String(32), nullable=True)
    identity_audit_reason = Column(String(255), nullable=True)
    identity_audit_at = Column(DateTime, nullable=True)

    # Telegram may block profile mutations while session/health/story layers differ (e.g. method unavailable for "frozen").
    profile_capability_status = Column(String(20), nullable=True)  # unknown | allowed | restricted
    profile_capability_reason = Column(String(255), nullable=True)

    # Rate limiting counters
    stories_today = Column(Integer, default=0)
    actions_today = Column(Integer, default=0)
    last_action_at = Column(DateTime, nullable=True)

    # Warmup and story precheck (separate from health; alive != story-ready)
    imported_at = Column(DateTime, nullable=True)  # When session was last imported/refreshed
    first_seen_at = Column(DateTime, nullable=True)  # First import or created_at
    last_story_attempt_at = Column(DateTime, nullable=True)
    last_story_failure_at = Column(DateTime, nullable=True)  # Set on failure; cooldown depends only on this
    last_story_success_at = Column(DateTime, nullable=True)  # Set on success; for audit
    story_attempts_today = Column(Integer, default=0)  # Incremented on attempt; reset daily
    successful_story_count = Column(Integer, default=0)
    failed_story_count = Column(Integer, default=0)
    warmup_status = Column(String(20), nullable=True)  # new | warming | warmed | risky | blocked
    story_precheck_status = Column(String(30), nullable=True)  # unknown | allowed | rate_limited | frozen | restricted | telegram_denied | blocked | not_warmed | failed_check
    story_precheck_reason = Column(String(255), nullable=True)
    story_precheck_checked_at = Column(DateTime, nullable=True)
    import_source = Column(String(100), nullable=True)  # tdata_zip | paste | qr
    risk_notes = Column(Text, nullable=True)

    # Purpose: autostory (stories), messaging (scheduler), both
    purpose = Column(String(20), default="both", nullable=False)

    # Metadata
    proxy_config = Column(JSON, nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    stories = relationship("Story", back_populates="account")
    tasks = relationship("Task", back_populates="account")

    def __repr__(self):
        status_str = self.status.value if self.status else "unknown"
        return f"<Account {self.phone_number} ({status_str})>"


class DiscoveredUser(Base):
    """Users discovered from public chats"""
    __tablename__ = "discovered_users"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, unique=True, nullable=False, index=True)
    username = Column(String(100), nullable=True, index=True)
    first_name = Column(String(100), nullable=True)
    last_name = Column(String(100), nullable=True)

    # Discovery info
    source_chat_id = Column(Integer, nullable=True)
    source_chat_title = Column(String(255), nullable=True)
    source_chat_username = Column(String(255), nullable=True, index=True)  # group @username for filtering
    discovered_at = Column(DateTime, default=datetime.utcnow)

    # Engagement tracking
    times_mentioned = Column(Integer, default=0)
    last_mentioned_at = Column(DateTime, nullable=True)
    is_blocked = Column(Boolean, default=False)

    # Tags for categorization
    tags = Column(JSON, default=list)

    def __repr__(self):
        return f"<DiscoveredUser {self.user_id} @{self.username}>"


class QrLoginToken(Base):
    """Persisted QR-login token/state so it survives restarts."""
    __tablename__ = "qr_login_tokens"

    token = Column(String(64), primary_key=True)
    status = Column(String(20), nullable=False, default="starting")  # starting/waiting/success/expired/error
    url = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    expires_at = Column(DateTime, nullable=False, index=True)


class HealthcheckRun(Base):
    """Audit + throttle for operator-triggered healthchecks. Background jobs store results/progress here."""
    __tablename__ = "healthcheck_runs"

    id = Column(Integer, primary_key=True, index=True)
    started_at = Column(DateTime, default=datetime.utcnow, index=True)
    finished_at = Column(DateTime, nullable=True, index=True)
    status = Column(String(20), default="running")  # running/success/error/timeout
    requested_by = Column(String(120), nullable=True)
    summary = Column(Text, nullable=True)
    # Background job fields (nullable for existing rows)
    results = Column(JSON, nullable=True)  # List[dict] of per-account results
    progress = Column(JSON, nullable=True)  # {"checked": N, "total": M}
    error_message = Column(Text, nullable=True)


class Campaign(Base):
    """Marketing campaign configuration"""
    __tablename__ = "campaigns"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)

    # Campaign settings
    is_active = Column(Boolean, default=True)
    start_date = Column(DateTime, nullable=True)
    end_date = Column(DateTime, nullable=True)

    # Targeting
    target_chat_ids = Column(JSON, default=list)  # Chats to discover users from
    mention_limit_per_user = Column(Integer, default=1)  # Max mentions per user

    # Content
    story_templates = Column(JSON, default=list)  # Story content templates
    media_files = Column(JSON, default=list)  # Associated media files

    # Statistics
    total_stories_published = Column(Integer, default=0)
    total_users_mentioned = Column(Integer, default=0)

    # Metadata
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    stories = relationship("Story", back_populates="campaign")
    tasks = relationship("Task", back_populates="campaign")

    def __repr__(self):
        return f"<Campaign {self.name}>"


class Story(Base):
    """Published story record"""
    __tablename__ = "stories"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    campaign_id = Column(Integer, ForeignKey("campaigns.id"), nullable=True)

    # Story content
    media_type = Column(String(20), nullable=False)  # photo, video
    media_path = Column(String(500), nullable=True)
    caption = Column(Text, nullable=True)

    # Mentions
    mentioned_user_ids = Column(JSON, default=list)
    mentioned_usernames = Column(JSON, default=list)

    # Story metadata from Telegram
    story_id = Column(Integer, nullable=True)  # Telegram story ID
    views_count = Column(Integer, default=0)

    # Status
    published_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)
    is_deleted = Column(Boolean, default=False)

    # Relationships
    account = relationship("Account", back_populates="stories")
    campaign = relationship("Campaign", back_populates="stories")

    def __repr__(self):
        return f"<Story {self.id} by Account {self.account_id}>"


class StoryBatchRun(Base):
    """Record of a story batch publish run for analytics."""
    __tablename__ = "story_batch_runs"

    id = Column(Integer, primary_key=True, index=True)
    started_at = Column(DateTime, default=datetime.utcnow, index=True)
    finished_at = Column(DateTime, nullable=True)

    config = Column(JSON, nullable=True)  # source_type, source_id, max_stories, mentions_per_story, etc.
    total_attempted = Column(Integer, default=0)
    successful = Column(Integer, default=0)
    failed = Column(Integer, default=0)
    skipped_count = Column(Integer, default=0)
    errors_json = Column(JSON, nullable=True)  # list of error strings
    mention_pool_size = Column(Integer, nullable=True)


class StoryPool(Base):
    """Logical pool of accounts for story rotation (existing story_pools table)."""
    __tablename__ = "story_pools"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    slug = Column(String(50), nullable=False, index=True)
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class StoryPoolMember(Base):
    """Membership of an account in a story pool."""
    __tablename__ = "story_pool_members"

    id = Column(Integer, primary_key=True, index=True)
    pool_id = Column(Integer, ForeignKey("story_pools.id"), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False, index=True)
    is_enabled = Column(Boolean, default=True)
    added_at = Column(DateTime, default=datetime.utcnow)


class StoryRun(Base):
    """Queued or running story rotation run (existing story_runs table)."""
    __tablename__ = "story_runs"

    id = Column(Integer, primary_key=True, index=True)
    pool_id = Column(Integer, ForeignKey("story_pools.id"), nullable=True, index=True)
    mode = Column(String(20), nullable=True)
    interval_minutes = Column(Integer, nullable=True)
    caption = Column(Text, nullable=True)
    media_path = Column(String(500), nullable=True)
    mentions_per_story = Column(Integer, nullable=True)
    max_stories = Column(Integer, nullable=True)
    status = Column(String(20), nullable=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    last_tick_at = Column(DateTime, nullable=True)
    next_tick_at = Column(DateTime, nullable=True)
    stories_ok = Column(Integer, default=0)
    stories_failed = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class StoryTemplate(Base):
    """Saved batch publish configuration for reuse."""
    __tablename__ = "story_templates"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)

    caption = Column(Text, nullable=True)
    mentions_per_story = Column(Integer, default=5)
    max_stories = Column(Integer, default=10)
    mention_source_type = Column(String(50), default="discovery")  # discovery, all, uploaded_file
    mention_source_id = Column(String(255), nullable=True)  # source username or uploaded source id
    avoid_reuse_days = Column(Integer, default=0)
    pool_behavior = Column(String(50), default="stop_batch")  # stop_batch, continue_with_less_mentions, fallback_to_all_discovered

    only_alive = Column(Boolean, default=True)
    skip_flood_wait = Column(Boolean, default=True)
    purpose_filter = Column(String(20), default="both")
    max_accounts = Column(Integer, nullable=True)
    daily_cap_per_account = Column(Integer, nullable=True)
    unique_mentions_across_batch = Column(Boolean, default=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class MentionBlacklist(Base):
    """Users to exclude from all mention pools."""
    __tablename__ = "mention_blacklist"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=True, index=True)
    username = Column(String(255), nullable=True, index=True)
    reason = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class UploadedMentionSource(Base):
    """Saved list of usernames/user IDs from an uploaded file."""
    __tablename__ = "uploaded_mention_sources"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    entries = relationship("UploadedMentionEntry", back_populates="source", cascade="all, delete-orphan")


class UploadedMentionEntry(Base):
    """Single user entry in an uploaded mention source (user_id or username)."""
    __tablename__ = "uploaded_mention_entries"

    id = Column(Integer, primary_key=True, index=True)
    source_id = Column(Integer, ForeignKey("uploaded_mention_sources.id"), nullable=False)
    user_id = Column(Integer, nullable=True, index=True)
    username = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    source = relationship("UploadedMentionSource", back_populates="entries")


class StorySchedule(Base):
    """Scheduled run of a story batch (uses template + optional media)."""
    __tablename__ = "story_schedules"

    id = Column(Integer, primary_key=True, index=True)
    story_template_id = Column(Integer, ForeignKey("story_templates.id"), nullable=False)
    name = Column(String(255), nullable=True)
    run_at = Column(DateTime, nullable=False, index=True)
    repeat = Column(String(50), nullable=True)  # null = once, "daily", "weekly"
    media_path = Column(String(500), nullable=True)
    max_accounts = Column(Integer, nullable=True)
    is_enabled = Column(Boolean, default=True)
    last_run_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Task(Base):
    """Task queue item"""
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id"), nullable=True)

    # Task definition
    task_type = Column(SQLEnum(TaskType), nullable=False)
    priority = Column(Integer, default=5)  # 1-10, lower is higher priority
    payload = Column(JSON, default=dict)  # Task-specific data

    # Scheduling
    scheduled_at = Column(DateTime, nullable=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    # Status
    status = Column(SQLEnum(TaskStatus), default=TaskStatus.PENDING)
    retry_count = Column(Integer, default=0)
    max_retries = Column(Integer, default=3)

    # Results
    result = Column(JSON, nullable=True)
    error_message = Column(Text, nullable=True)

    # Celery integration
    celery_task_id = Column(String(255), nullable=True, index=True)

    # Metadata
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    account = relationship("Account", back_populates="tasks")
    campaign = relationship("Campaign", back_populates="tasks")

    def __repr__(self):
        return f"<Task {self.id} {self.task_type.value} ({self.status.value})>"


class SystemLog(Base):
    """System activity log"""
    __tablename__ = "system_logs"

    id = Column(Integer, primary_key=True, index=True)
    level = Column(String(20), nullable=False)  # INFO, WARNING, ERROR
    component = Column(String(100), nullable=False)
    message = Column(Text, nullable=False)
    details = Column(JSON, nullable=True)

    # Related entities
    account_id = Column(Integer, nullable=True)
    task_id = Column(Integer, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<SystemLog {self.level} {self.component}>"


# Import scheduler models so they're registered with Base
from . import scheduler_models  # noqa: F401, E402
