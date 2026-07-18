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
    session_string = Column(Text, nullable=True)  # Encrypted session data

    # Account info
    user_id = Column(Integer, nullable=True, index=True)  # Telegram user ID
    username = Column(String(100), nullable=True)
    first_name = Column(String(100), nullable=True)
    last_name = Column(String(100), nullable=True)

    # Status and health
    status = Column(SQLEnum(AccountStatus, values_callable=lambda obj: [e.value for e in obj]),
                    default=AccountStatus.AUTH_REQUIRED)
    last_active = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    flood_wait_until = Column(DateTime, nullable=True)

    # Telegram health check results (written by fleet health check jobs)
    health_status = Column(String(50), nullable=True)       # alive / banned / frozen / restricted / auth_required / flood_wait / deleted
    health_reason = Column(Text, nullable=True)             # human-readable summary from last health check
    health_checked_at = Column(DateTime, nullable=True)     # UTC timestamp of last health check

    # Story precheck results (written by /api/accounts/story-precheck)
    story_precheck_status = Column(String(50), nullable=True)   # allowed / frozen / not_authorized
    story_precheck_reason = Column(String(255), nullable=True)
    story_precheck_checked_at = Column(DateTime, nullable=True) # UTC timestamp of last story precheck
    story_status = Column(String(20), nullable=True)
    story_status_reason = Column(String(255), nullable=True)
    story_status_checked_at = Column(DateTime, nullable=True)
    story_blocked_until = Column(DateTime, nullable=True)
    last_story_attempt_at = Column(DateTime, nullable=True)
    last_story_success_at = Column(DateTime, nullable=True)

    # Account purpose — controls which subsystems use this account
    # autostory | messaging | both | ai_agent (ai_agent: negotiation desk only, no scheduler/stories)
    purpose = Column(String(20), nullable=True, default="both")

    # Rate limiting counters
    stories_today = Column(Integer, default=0)
    actions_today = Column(Integer, default=0)
    last_action_at = Column(DateTime, nullable=True)

    # Metadata
    proxy_config = Column(JSON, nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    stories = relationship("Story", back_populates="account")
    tasks = relationship("Task", back_populates="account")

    def __repr__(self):
        return f"<Account {self.phone_number} ({self.status.value})>"


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
    discovered_at = Column(DateTime, default=datetime.utcnow)

    # Engagement tracking
    times_mentioned = Column(Integer, default=0)
    last_mentioned_at = Column(DateTime, nullable=True)
    is_blocked = Column(Boolean, default=False)

    # Tags for categorization
    tags = Column(JSON, default=list)

    def __repr__(self):
        return f"<DiscoveredUser {self.user_id} @{self.username}>"


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


# ──────────────────────────────────────────────────────────────────────────────
# Story Rotation Models
# ──────────────────────────────────────────────────────────────────────────────

from sqlalchemy import UniqueConstraint  # noqa: E402 (needed for StoryPoolMember)


class StoryPool(Base):
    """
    Named pool of accounts dedicated to story publishing.
    Example: armcoinstory, warmup_pool, backup_pool
    """
    __tablename__ = "story_pools"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False)
    slug = Column(String(50), unique=True, nullable=False)   # armcoinstory
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    members = relationship("StoryPoolMember", back_populates="pool",
                           cascade="all, delete-orphan")
    runs = relationship("StoryRun", back_populates="pool")

    def __repr__(self):
        return f"<StoryPool {self.slug}>"


class StoryPoolMember(Base):
    """Account membership in a story pool."""
    __tablename__ = "story_pool_members"

    id = Column(Integer, primary_key=True, index=True)
    pool_id = Column(Integer, ForeignKey("story_pools.id"), nullable=False)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    is_enabled = Column(Boolean, default=True)
    added_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("pool_id", "account_id",
                                       name="uq_pool_account"),)

    pool = relationship("StoryPool", back_populates="members")
    account = relationship("Account")

    def __repr__(self):
        return f"<StoryPoolMember pool={self.pool_id} account={self.account_id}>"


class StoryRun(Base):
    """
    A story rotation run — one-shot batch or continuous rotation.
    The worker picks this up and executes it.
    """
    __tablename__ = "story_runs"

    id = Column(Integer, primary_key=True, index=True)
    pool_id = Column(Integer, ForeignKey("story_pools.id"), nullable=True)

    # Configuration (immutable after creation)
    mode = Column(String(20), default="once")          # once | continuous
    interval_minutes = Column(Integer, nullable=True)  # continuous only
    caption = Column(Text, nullable=True)
    media_path = Column(String(500), nullable=True)
    mentions_per_story = Column(Integer, default=5)
    max_stories = Column(Integer, nullable=True)       # None = unlimited
    mention_source_chat_id = Column(Integer, nullable=True)  # None = all groups
    # Approved mention plan from Dry Run (list of {user_id, username, ...}); immutable intent.
    mention_plan = Column(JSON, nullable=True)

    # State (updated by worker)
    status = Column(String(20), default="pending")     # pending|running|completed|failed|cancelled
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    last_tick_at = Column(DateTime, nullable=True)     # last rotation step
    next_tick_at = Column(DateTime, nullable=True)     # next scheduled step

    # Counters
    stories_ok = Column(Integer, default=0)
    stories_failed = Column(Integer, default=0)

    created_at = Column(DateTime, default=datetime.utcnow)

    pool = relationship("StoryPool", back_populates="runs")
    steps = relationship("StoryRunStep", back_populates="run",
                         cascade="all, delete-orphan")

    def __repr__(self):
        return f"<StoryRun {self.id} {self.mode} {self.status}>"


class StoryRunStep(Base):
    """One story publish attempt within a StoryRun."""
    __tablename__ = "story_run_steps"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(Integer, ForeignKey("story_runs.id"), nullable=False)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    story_id = Column(Integer, ForeignKey("stories.id"), nullable=True)

    status = Column(String(20), default="pending")  # ok | failed | skipped
    error = Column(Text, nullable=True)
    executed_at = Column(DateTime, default=datetime.utcnow)

    run = relationship("StoryRun", back_populates="steps")
    account = relationship("Account")

    def __repr__(self):
        return f"<StoryRunStep run={self.run_id} account={self.account_id} {self.status}>"


# Import scheduler models so they're registered with Base
from . import scheduler_models  # noqa: F401, E402
