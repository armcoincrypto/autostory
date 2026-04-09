"""
STORYFLEET Configuration Settings
Centralized configuration management using Pydantic
"""
from typing import Optional, List
from pydantic_settings import BaseSettings
from pydantic import Field
import os
from dotenv import load_dotenv

# Load .env file explicitly
load_dotenv()


class TelegramSettings(BaseSettings):
    """Telegram API configuration"""
    api_id: int = Field(default=0, description="Telegram API ID from my.telegram.org")
    api_hash: str = Field(default="", description="Telegram API Hash from my.telegram.org")

    # Rate limiting
    min_delay_between_actions: float = Field(default=2.0, description="Minimum delay between actions (seconds)")
    max_delay_between_actions: float = Field(default=5.0, description="Maximum delay between actions (seconds)")
    max_stories_per_hour: int = Field(default=10, description="Maximum stories per account per hour")
    max_mentions_per_story: int = Field(default=5, description="Maximum mentions per story")

    # Anti-detection
    randomize_delays: bool = Field(default=True, description="Add random variance to delays")
    simulate_typing: bool = Field(default=True, description="Simulate typing behavior")

    class Config:
        env_prefix = "TELEGRAM_"


class DatabaseSettings(BaseSettings):
    """Database configuration"""
    url: str = Field(
        default="sqlite:///./data/storyfleet.db",
        description="Database connection URL"
    )
    pool_size: int = Field(default=5, description="Connection pool size")
    max_overflow: int = Field(default=10, description="Max overflow connections")

    class Config:
        env_prefix = "DATABASE_"


class RedisSettings(BaseSettings):
    """Redis configuration for caching and task queue"""
    host: str = Field(default="localhost", description="Redis host")
    port: int = Field(default=6379, description="Redis port")
    db: int = Field(default=0, description="Redis database number")
    password: Optional[str] = Field(default=None, description="Redis password")

    @property
    def url(self) -> str:
        if self.password:
            return f"redis://:{self.password}@{self.host}:{self.port}/{self.db}"
        return f"redis://{self.host}:{self.port}/{self.db}"

    class Config:
        env_prefix = "REDIS_"


class DashboardSettings(BaseSettings):
    """Flask dashboard configuration"""
    secret_key: str = Field(default="change-me-in-production", description="Flask secret key")
    host: str = Field(default="0.0.0.0", description="Dashboard host")
    port: int = Field(default=5000, description="Dashboard port")
    debug: bool = Field(default=False, description="Enable debug mode")

    class Config:
        env_prefix = "DASHBOARD_"


class BotSettings(BaseSettings):
    """Telegram Bot Dashboard configuration"""
    token: str = Field(default="", description="Telegram Bot Token")
    admin_ids: List[int] = Field(default_factory=list, description="Authorized admin user IDs")

    class Config:
        env_prefix = "BOT_"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Fallback to direct env var reading
        if not self.token:
            self.token = os.getenv("BOT_TOKEN", "")
        if not self.admin_ids:
            admin_str = os.getenv("BOT_ADMIN_IDS", "[]")
            try:
                import json
                self.admin_ids = json.loads(admin_str)
            except:
                self.admin_ids = []


class StorageSettings(BaseSettings):
    """File storage configuration"""
    sessions_dir: str = Field(default="./data/sessions", description="Session files directory")
    media_dir: str = Field(default="./data/media", description="Media files directory")
    logs_dir: str = Field(default="./data/logs", description="Log files directory")

    class Config:
        env_prefix = "STORAGE_"


class WarmupSettings(BaseSettings):
    """Story warmup / precheck configuration"""
    # How long (minutes) a story-precheck result stays valid after being written.
    # Default 1440 = 24 h so operators can run precheck once per day.
    precheck_ttl_post_minutes: int = Field(default=1440, ge=1, description="Story precheck result TTL after writing (minutes)")
    # General precheck TTL used elsewhere (kept for back-compat with older code)
    precheck_ttl_minutes: int = Field(default=1440, ge=1, description="Story precheck result TTL (minutes)")
    max_prechecks_per_hour: int = Field(default=20, ge=1, description="Max story prechecks per hour across all accounts")
    canary_batch_ok_required: bool = Field(default=True, description="Require canary_batch_ok=true flag for batch precheck")

    class Config:
        env_prefix = "WARMUP_"


class Settings(BaseSettings):
    """Main application settings"""
    app_name: str = Field(default="STORYFLEET", description="Application name")
    environment: str = Field(default="development", description="Environment (development/production)")
    log_level: str = Field(default="INFO", description="Logging level")

    # Sub-configurations - load them explicitly
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    dashboard: DashboardSettings = Field(default_factory=DashboardSettings)
    bot: BotSettings = Field(default_factory=BotSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    warmup: WarmupSettings = Field(default_factory=WarmupSettings)

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


# Global settings instance
settings = Settings()
