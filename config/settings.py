"""
STORYFLEET Configuration Settings
Centralized configuration management using Pydantic v2 / pydantic-settings.
"""
from __future__ import annotations

import json
import os
from typing import Any, List, Optional

from dotenv import load_dotenv
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv()


class TelegramSettings(BaseSettings):
    """Telegram API configuration"""

    model_config = SettingsConfigDict(env_prefix="TELEGRAM_", extra="ignore")

    api_id: int = Field(default=0, description="Telegram API ID from my.telegram.org")
    api_hash: str = Field(default="", description="Telegram API Hash from my.telegram.org")
    min_delay_between_actions: float = Field(default=2.0)
    max_delay_between_actions: float = Field(default=5.0)
    max_stories_per_hour: int = Field(default=10)
    max_mentions_per_story: int = Field(default=5)
    randomize_delays: bool = Field(default=True)
    simulate_typing: bool = Field(default=True)


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DATABASE_", extra="ignore")

    url: str = Field(default="sqlite:///./data/storyfleet.db")
    pool_size: int = Field(default=5)
    max_overflow: int = Field(default=10)


class RedisSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="REDIS_", extra="ignore")

    host: str = Field(default="localhost")
    port: int = Field(default=6379)
    db: int = Field(default=0)
    password: Optional[str] = Field(default=None)

    @property
    def url(self) -> str:
        if self.password:
            return f"redis://:{self.password}@{self.host}:{self.port}/{self.db}"
        return f"redis://{self.host}:{self.port}/{self.db}"


class DashboardSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DASHBOARD_", extra="ignore")

    secret_key: str = Field(default="change-me-in-production")
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=5000)
    debug: bool = Field(default=False)
    run_now_proxy_url: Optional[str] = Field(default=None)
    admin_token: Optional[str] = Field(default=None, description="X-Admin-Token value in production")
    allow_insecure_admin_api: bool = Field(
        default=False,
        description="If true, admin API routes skip token check (dev only)",
    )


class BotSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BOT_", extra="ignore")

    token: str = Field(default="")
    admin_ids: List[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def fallback_bot_env(self) -> "BotSettings":
        if not self.token:
            object.__setattr__(self, "token", os.getenv("BOT_TOKEN", "") or "")
        if not self.admin_ids:
            raw = os.getenv("BOT_ADMIN_IDS", "[]") or "[]"
            try:
                object.__setattr__(self, "admin_ids", json.loads(raw))
            except (json.JSONDecodeError, TypeError):
                object.__setattr__(self, "admin_ids", [])
        return self


class StorageSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STORAGE_", extra="ignore")

    sessions_dir: str = Field(default="./data/sessions")
    media_dir: str = Field(default="./data/media")
    logs_dir: str = Field(default="./data/logs")


class WarmupSettings(BaseSettings):
    """Story warmup, precheck TTL, bulk profile caps (env prefix WARMUP_)."""

    model_config = SettingsConfigDict(env_prefix="WARMUP_", extra="ignore")

    enabled: bool = True
    min_account_age_hours: int = Field(default=24, ge=0)
    story_failure_cooldown_minutes: int = Field(default=120, ge=0)
    max_story_attempts_per_day: int = Field(default=5, ge=0)
    max_story_successes_per_day: int = Field(default=3, ge=0)
    jitter_min_sec: int = Field(default=10, ge=0)
    jitter_max_sec: int = Field(default=90, ge=0)
    precheck_ttl_post_minutes: int = Field(default=15, ge=1)
    precheck_ttl_minutes: int = Field(default=1440, ge=1)
    canary_default_batch_size: int = Field(default=1, ge=1)
    canary_batch_ok_required: bool = True
    max_prechecks_per_hour: int = Field(default=20, ge=1)
    max_bulk_username_batch: int = Field(default=5, ge=0)
    max_bulk_photo_batch: int = Field(default=3, ge=0)
    max_bulk_profile_actions_per_hour: int = Field(default=8, ge=0)
    max_username_changes_per_hour: int = Field(default=5, ge=0)
    max_profile_photo_changes_per_hour: int = Field(default=3, ge=0)
    max_accounts_per_story_batch: int = Field(default=10, ge=1)
    max_warming_accounts_per_bulk: int = Field(default=0, ge=0)
    username_change_cooldown_hours: float = Field(default=6.0, ge=0)
    profile_photo_change_cooldown_hours: float = Field(default=12.0, ge=0)


class Settings(BaseSettings):
    """
    Main application settings.

    Top-level ``extra="ignore"``: unknown env vars must not crash Gunicorn; scheduler
    and other subsystems may add `.env` keys before settings models catch up.

    ``sched_default_*``: dashboard/scheduler defaults (production `.env` uses these names;
    pydantic-settings matches them case-insensitively).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        nested_model_default_partial_update=True,
    )

    app_name: str = Field(default="STORYFLEET")
    environment: str = Field(default="development")
    log_level: str = Field(default="INFO")

    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    dashboard: DashboardSettings = Field(default_factory=DashboardSettings)
    bot: BotSettings = Field(default_factory=BotSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    warmup: WarmupSettings = Field(default_factory=WarmupSettings)

    # -------------------------------------------------------------------------
    # Scheduler defaults (flat fields — env keys sched_default_* / SCHED_DEFAULT_*)
    # -------------------------------------------------------------------------
    sched_default_timezone: str = Field(
        default="Europe/Moscow",
        description="Default IANA timezone for new schedule profiles / UI",
    )
    sched_default_min_interval_sec: int = Field(default=300, ge=0)
    sched_default_daily_total: int = Field(default=20, ge=0)
    sched_default_daily_promo: int = Field(default=5, ge=0)
    sched_default_daily_info: int = Field(default=5, ge=0)
    sched_default_jitter_sec: int = Field(default=0, ge=0)
    sched_default_quiet_hours_json: Optional[str] = Field(
        default=None,
        description=(
            "JSON string for ScheduleProfile.quiet_hours_json (object or [] as empty/disabled). "
            "Stored as str to match DB/Text usage; consumers use json.loads."
        ),
    )
    sched_default_rule_promo_times_json: Optional[str] = Field(
        default=None,
        description="JSON array string for promo rule times_json (e.g. [\"10:00\",\"RANDOM:..\"]).",
    )
    sched_default_rule_info_times_json: Optional[str] = Field(
        default=None,
        description="JSON array string for info rule times_json.",
    )

    @staticmethod
    def _normalize_sched_json_env(v: Any) -> Any:
        """
        .env values are strings; pydantic-settings may occasionally pass decoded list/dict.
        Normalize to a compact JSON str so types match DB JSON-in-Text expectations.
        Invalid JSON strings are kept as-is so boot never fails on marginally quoted .env lines.
        """
        if v is None:
            return None
        if isinstance(v, (list, dict)):
            return json.dumps(v, ensure_ascii=False)
        if not isinstance(v, str):
            return v
        s = v.strip()
        if not s:
            return None
        try:
            parsed = json.loads(s)
        except (json.JSONDecodeError, TypeError):
            return s
        if isinstance(parsed, (list, dict)):
            return json.dumps(parsed, ensure_ascii=False)
        return s

    @field_validator(
        "sched_default_quiet_hours_json",
        "sched_default_rule_promo_times_json",
        "sched_default_rule_info_times_json",
        mode="before",
    )
    @classmethod
    def sched_json_from_env(cls, v: Any) -> Any:
        return cls._normalize_sched_json_env(v)


settings = Settings()
