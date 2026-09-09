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
    fleet_operational_state_api_enabled: bool = Field(
        default=False,
        description=(
            "Enable GET /api/accounts/operational-state (read-only fleet truth). "
            "Env: FLEET_OPERATIONAL_STATE_API_ENABLED."
        ),
    )
    readiness_v2_api_enabled: bool = Field(
        default=False,
        description=(
            "Enable GET /api/readiness/v2/snapshots (read-only v2 observer). "
            "Env: READINESS_V2_API_ENABLED. v1 remains authoritative."
        ),
    )
    accounts_v2_dashboard_enabled: bool = Field(
        default=False,
        description=(
            "Enable GET /accounts-v2 dashboard shell (read-only preview). "
            "Env: ACCOUNTS_V2_DASHBOARD_ENABLED. v1 dashboard unchanged."
        ),
    )
    scheduler_mutations_enabled: bool = Field(
        default=False,
        description=(
            "When false (default), /api/v1 scheduler POST/PUT/DELETE mutation routes "
            "return HTTP 423 before DB writes, queue enqueue, or Telegram. "
            "Env: SCHEDULER_MUTATIONS_ENABLED."
        ),
    )
    messages_execution_enabled: bool = Field(
        default=False,
        description=(
            "When false (default), owner direct-message live sends are denied. "
            "Independent of SCHEDULER_MUTATIONS_ENABLED. Dry-run remains allowed. "
            "Env: MESSAGES_EXECUTION_ENABLED."
        ),
    )
    # -------------------------------------------------------------------------
    # Owner Messages — AI draft assistant (Wave 7E-OAI; draft only, never send)
    # -------------------------------------------------------------------------
    messages_ai_draft_enabled: bool = Field(
        default=False,
        description=(
            "When false (default), Draft with AI is denied. Independent of "
            "MESSAGES_EXECUTION_ENABLED. Env: MESSAGES_AI_DRAFT_ENABLED."
        ),
    )
    messages_ai_draft_model: str = Field(
        default="gpt-5.6-luna",
        description=(
            "OpenAI model id for owner Messages drafting "
            "(env MESSAGES_AI_DRAFT_MODEL). Uses canonical OPENAI_API_KEY."
        ),
    )
    messages_ai_draft_timeout_sec: int = Field(
        default=20,
        ge=1,
        le=120,
        description="HTTP timeout seconds for AI draft calls (env MESSAGES_AI_DRAFT_TIMEOUT_SEC).",
    )
    messages_ai_draft_max_output_tokens: int = Field(
        default=512,
        ge=64,
        le=4096,
        description="Max output tokens for AI drafts (env MESSAGES_AI_DRAFT_MAX_OUTPUT_TOKENS).",
    )
    messages_ai_draft_max_context_messages: int = Field(
        default=20,
        ge=1,
        le=50,
        description=(
            "Max recent messages sent to AI as context "
            "(env MESSAGES_AI_DRAFT_MAX_CONTEXT_MESSAGES)."
        ),
    )
    messages_ai_draft_max_input_chars: int = Field(
        default=12000,
        ge=500,
        le=100000,
        description=(
            "Max total conversation text chars for AI context "
            "(env MESSAGES_AI_DRAFT_MAX_INPUT_CHARS)."
        ),
    )
    scheduler_mutation_account_allowlist: str = Field(
        default="",
        description=(
            "Comma-separated account IDs allowed for scoped mutation bypass when "
            "SCHEDULER_MUTATION_SCOPE=send_test_only and global mutations stay false. "
            "Env: SCHEDULER_MUTATION_ACCOUNT_ALLOWLIST."
        ),
    )
    scheduler_mutation_scope: str = Field(
        default="",
        description=(
            "Scoped mutation mode: 'send_test_only' or 'campaign_pilot_5' permits "
            "POST /api/v1/jobs/run-now for allowlisted accounts (and targets for pilot). "
            "Env: SCHEDULER_MUTATION_SCOPE."
        ),
    )
    scheduler_mutation_target_allowlist: str = Field(
        default="",
        description=(
            "Comma-separated target IDs for campaign_pilot_5 scope (run-now target_id must match). "
            "Env: SCHEDULER_MUTATION_TARGET_ALLOWLIST."
        ),
    )
    scheduler_mutation_pair_allowlist: str = Field(
        default="",
        description=(
            "Comma-separated account:target pairs for campaign_pilot_5 "
            "(e.g. 111:17,109:16). When set, run-now must match an exact pair. "
            "Env: SCHEDULER_MUTATION_PAIR_ALLOWLIST."
        ),
    )
    campaign_execution_enabled: bool = Field(
        default=False,
        description=(
            "When false (default), governed campaign sends (campaign_pilot_5 marker) "
            "are blocked even if scoped allowlists are active. Requires armed "
            "CampaignGovernance record when true. Env: CAMPAIGN_EXECUTION_ENABLED."
        ),
    )
    story_execution_enabled: bool = Field(
        default=False,
        description=(
            "When false (default), live story publish and rotation worker ticks are blocked. "
            "Env: STORY_EXECUTION_ENABLED."
        ),
    )
    discovery_execution_enabled: bool = Field(
        default=False,
        description=(
            "When false (default), live discovery scan/join Telethon actions are blocked. "
            "Dry-run discovery remains allowed. Env: DISCOVERY_EXECUTION_ENABLED."
        ),
    )
    p4c_single_send_enabled: bool = Field(
        default=False,
        description=(
            "P4C scoped controlled send: when true, allows exactly one live send via "
            "send_test_only scope for allowlisted account (controlled runner only). "
            "Env: P4C_SINGLE_SEND_ENABLED."
        ),
    )
    p4c_single_send_max: int = Field(
        default=1,
        description="Max live P4C sends while P4C_SINGLE_SEND_ENABLED=true. Env: P4C_SINGLE_SEND_MAX.",
    )
    p5a_single_send_enabled: bool = Field(
        default=False,
        description=(
            "P5A scoped repeatability pilot: when true, allows one live send via manifest. "
            "Env: P5A_SINGLE_SEND_ENABLED."
        ),
    )
    p5a_single_send_max: int = Field(
        default=1,
        description="Max live P5A sends while P5A_SINGLE_SEND_ENABLED=true. Env: P5A_SINGLE_SEND_MAX.",
    )
    p5c_single_send_enabled: bool = Field(
        default=False,
        description=(
            "P5C scoped second-target pilot: when true, allows one live send via manifest "
            "through gateway. Env: P5C_SINGLE_SEND_ENABLED."
        ),
    )
    p5c_single_send_max: int = Field(
        default=1,
        description="Max live P5C sends while P5C_SINGLE_SEND_ENABLED=true. Env: P5C_SINGLE_SEND_MAX.",
    )
    p5d_single_send_enabled: bool = Field(
        default=False,
        description="P5D gateway restart durability certification. Env: P5D_SINGLE_SEND_ENABLED.",
    )
    production_certified_no_go: bool = Field(
        default=True,
        description=(
            "When true (default), production remains NO_GO and normal PROMO generation is denied. "
            "Env: AUTOSTORY_PRODUCTION_CERTIFIED_NO_GO."
        ),
    )
    promo_generation_mode: str = Field(
        default="disabled",
        description=(
            "Background PROMO/INFO generation mode: disabled | planning_only | executable. "
            "Unknown or empty values deny generation. Env: PROMO_GENERATION_MODE."
        ),
    )
    dashboard_inject_admin_token: bool = Field(
        default=False,
        description=(
            "When false (default), admin token is not embedded in HTML page source. "
            "Use session auth or X-Admin-Token header. Env: DASHBOARD_INJECT_ADMIN_TOKEN."
        ),
    )
    dashboard_allow_query_admin_token: bool = Field(
        default=False,
        description=(
            "When false (default), admin_token query parameter is rejected. "
            "Prefer X-Admin-Token header. Env: DASHBOARD_ALLOW_QUERY_ADMIN_TOKEN."
        ),
    )

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
        default="Asia/Yerevan",
        description="Default IANA timezone for new schedule profiles / UI (env SCHED_DEFAULT_TIMEZONE).",
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

    # -------------------------------------------------------------------------
    # AI Agent (optional; keys never exposed via API or logs)
    # -------------------------------------------------------------------------
    ai_agent_provider: str = Field(
        default="fallback",
        description="AI draft provider: 'fallback' (deterministic) or 'openai'.",
    )
    ai_agent_use_openai: bool = Field(
        default=False,
        description=(
            "When true and OPENAI_API_KEY is set, call OpenAI for drafts even if "
            "ai_agent_provider is 'fallback'. Env AI_AGENT_USE_OPENAI (1/true/yes)."
        ),
    )
    ai_agent_model: str = Field(
        default="gpt-4o-mini",
        description="OpenAI chat model when using OpenAI (env AI_AGENT_MODEL).",
    )
    openai_model: str = Field(
        default="",
        description="Optional model override (env OPENAI_MODEL). When empty, ai_agent_model is used.",
    )
    ai_agent_openai_timeout_sec: int = Field(
        default=15,
        ge=1,
        le=120,
        description="HTTP timeout seconds for OpenAI chat completions (env AI_AGENT_OPENAI_TIMEOUT_SEC).",
    )
    openai_api_key: str = Field(
        default="",
        repr=False,
        description="OpenAI API key from env OPENAI_API_KEY (never logged or returned).",
    )
    ai_agent_account_phones: str = Field(
        default="",
        description=(
            "Comma-separated E.164 phones reserved for AI Agent (optional). "
            "When set (with or without AI_AGENT_ACCOUNT_IDS), only these accounts "
            "may use AI Agent Telegram I/O; scheduler treats them as reserved."
        ),
    )
    ai_agent_account_ids: str = Field(
        default="",
        description=(
            "Comma-separated numeric account ids reserved for AI Agent (optional). "
            "Merged with phones from AI_AGENT_ACCOUNT_PHONES."
        ),
    )
    ai_agent_default_target_premium_pct: float = Field(
        default=0.5,
        ge=0.0,
        le=100.0,
        description=(
            "OTC: counter-offer anchor when pushing back on seller premium (env "
            "AI_AGENT_DEFAULT_TARGET_PREMIUM_PCT)."
        ),
    )
    ai_agent_negotiate_rate_threshold_pct: float = Field(
        default=0.8,
        ge=0.0,
        le=100.0,
        description=(
            "OTC: ask for a better rate when seller premium %% is at or above this "
            "(env AI_AGENT_NEGOTIATE_RATE_THRESHOLD_PCT)."
        ),
    )
    # OTC profit policy (Phase 2) — stored in facts JSON only; no DB migration.
    ai_agent_otc_base_market_rate_source: str = Field(
        default="manual",
        description="Reserved: base market context for OTC (env AI_AGENT_OTC_BASE_MARKET_RATE_SOURCE).",
    )
    ai_agent_otc_target_buy_premium_pct: float = Field(
        default=0.3,
        ge=0.0,
        le=100.0,
        description="Preferred buy-side premium %% (lower is better) — env AI_AGENT_OTC_TARGET_BUY_PREMIUM_PCT.",
    )
    ai_agent_otc_max_buy_premium_pct: float = Field(
        default=0.8,
        ge=0.0,
        le=100.0,
        description="Max acceptable buy premium before strong push / review — env AI_AGENT_OTC_MAX_BUY_PREMIUM_PCT.",
    )
    ai_agent_otc_target_sell_premium_pct: float = Field(
        default=1.0,
        ge=0.0,
        le=100.0,
        description="Preferred sell-side premium %% (higher is better) — env AI_AGENT_OTC_TARGET_SELL_PREMIUM_PCT.",
    )
    ai_agent_otc_min_sell_premium_pct: float = Field(
        default=0.4,
        ge=0.0,
        le=100.0,
        description="Minimum acceptable sell premium before reject/review — env AI_AGENT_OTC_MIN_SELL_PREMIUM_PCT.",
    )
    ai_agent_otc_large_deal_amount: float = Field(
        default=2000.0,
        ge=0.0,
        description="Amount threshold (crypto units) for large tier — env AI_AGENT_OTC_LARGE_DEAL_AMOUNT.",
    )
    ai_agent_otc_small_deal_amount: float = Field(
        default=500.0,
        ge=0.0,
        description="Amount threshold for small tier — env AI_AGENT_OTC_SMALL_DEAL_AMOUNT.",
    )
    ai_agent_telegram_operator_ids: str = Field(
        default="",
        description=(
            "Comma-separated Telegram user IDs allowed to use /ai_* bot commands "
            "(env AI_AGENT_TELEGRAM_OPERATOR_IDS). Empty means no operators (commands disabled)."
        ),
    )
    ai_agent_default_account_id: int = Field(
        default=110,
        ge=1,
        description="Default AI Agent Telegram account for /ai_new (env AI_AGENT_DEFAULT_ACCOUNT_ID).",
    )
    kathleen_telegram_operator_ids: str = Field(
        default="",
        description=(
            "Comma-separated Telegram user IDs allowed to enable the Dexpert Controller listener "
            "(env KATHLEEN_TELEGRAM_OPERATOR_IDS). Only Telegram id 667100147 (@Armcryptoseller) "
            "is honored as the operator when non-empty. Empty disables the listener commands."
        ),
    )
    dexpert_owner_telegram_username: str = Field(
        default="",
        description=(
            "Telegram username (no @) for Dexpert ready_for_operator DMs when numeric id "
            "does not resolve in Telethon (env DEXPERT_OWNER_TELEGRAM_USERNAME)."
        ),
    )
    kathleen_telegram_operator_usernames: str = Field(
        default="",
        description=(
            "Comma-separated usernames (optional @) used as fallback peer for owner notify "
            "after DEXPERT_OWNER_TELEGRAM_USERNAME (env KATHLEEN_TELEGRAM_OPERATOR_USERNAMES). "
            "Non-numeric tokens only; first match wins."
        ),
    )
    kathleen_account_id: int = Field(
        default=0,
        ge=0,
        description=(
            "Telegram ``accounts.id`` used exclusively by Kathleen Account Listener user session "
            "(env KATHLEEN_ACCOUNT_ID). Use 0 when listener is off. Do not use AI pool 110/113/131 "
            "unless KATHLEEN_ALLOW_RESERVED_AI_POOL_ACCOUNTS is enabled."
        ),
    )
    kathleen_account_listener_enabled: bool = Field(
        default=False,
        description=(
            "When true, systemd may run ``python -m src.bot.kathleen_account_listener`` "
            "(env KATHLEEN_ACCOUNT_LISTENER_ENABLED)."
        ),
    )
    kathleen_allow_reserved_ai_pool_accounts: bool = Field(
        default=False,
        description=(
            "Allow Kathleen listener on reserved AI negotiation account ids 110/113/131 "
            "(env KATHLEEN_ALLOW_RESERVED_AI_POOL_ACCOUNTS). Default false to avoid gateway lock fights."
        ),
    )
    kathleen_session_lock_timeout_sec: float = Field(
        default=60.0,
        ge=1.0,
        le=600.0,
        description="Session flock acquire timeout for Kathleen listener (env KATHLEEN_SESSION_LOCK_TIMEOUT_SEC).",
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
