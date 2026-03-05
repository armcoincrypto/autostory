"""
STORYFLEET Configuration Settings
Centralized configuration management using Pydantic
"""
from typing import Optional, List
from pydantic_settings import BaseSettings
from pydantic import Field
import os
import secrets
from dotenv import load_dotenv

# Load .env file explicitly
load_dotenv()


def _generate_secret_key() -> str:
    """
    SECURITY FIX: Generate secure secret key if not provided

    Priority:
    1. FLASK_SECRET_KEY or DASHBOARD_SECRET_KEY from env
    2. Generate cryptographically secure key and warn
    """
    env_key = os.getenv("FLASK_SECRET_KEY") or os.getenv("DASHBOARD_SECRET_KEY")
    if env_key and len(env_key) >= 32:
        return env_key

    # Generate new key
    new_key = secrets.token_hex(32)
    print(f"WARNING: Generated new secret key. Set FLASK_SECRET_KEY in .env for persistence.")
    return new_key


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
    # SECURITY FIX: Use factory function to generate secure key
    secret_key: str = Field(
        default_factory=_generate_secret_key,
        description="Flask secret key (auto-generated if not set)"
    )
    host: str = Field(default="0.0.0.0", description="Dashboard host")
    port: int = Field(default=5000, description="Dashboard port")
    debug: bool = Field(default=False, description="Enable debug mode")
    run_now_proxy_url: Optional[str] = Field(
        default=None,
        description="When set (e.g. http://207.180.212.142:5000), Send test now proxies to server so sessions work"
    )

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

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


# Global settings instance
settings = Settings()
