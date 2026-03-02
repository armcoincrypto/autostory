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
    status = Column(SQLEnum(AccountStatus), default=AccountStatus.AUTH_REQUIRED)
    last_active = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    flood_wait_until = Column(DateTime, nullable=True)

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


# Import scheduler models so they're registered with Base
from . import scheduler_models  # noqa: F401, E402
