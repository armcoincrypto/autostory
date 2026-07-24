"""Integration status helpers (honest, no fake connected providers)."""
from __future__ import annotations

import os
from typing import Any

from src.social_agent.credential_crypto import SocialCredentialCrypto
from src.social_agent.providers.meta import MetaProviderAdapter, GRAPH_API_VERSION


def meta_status() -> dict[str, Any]:
    cfg = MetaProviderAdapter.config()
    crypto_ok = SocialCredentialCrypto.configured()
    if not cfg["configured"]:
        status = "CREDENTIALS_MISSING"
        message = (
            "Meta OAuth ready; configure Meta app ID, app secret, and HTTPS redirect URI via server environment."
        )
    elif not crypto_ok:
        status = "CREDENTIAL_KEY_MISSING"
        message = "Meta app credentials present; install SOCIAL_CREDENTIAL_* encryption keys before connecting."
    else:
        status = "BACKEND_READY"
        message = "Meta credentials and encryption configured; connect from Social Accounts."
    return {
        "provider": "meta",
        "status": status,
        "configured": bool(cfg["configured"] and crypto_ok),
        "app_credentials_present": bool(cfg["configured"]),
        "app_id_present": bool(cfg.get("app_id")),
        "redirect_uri_present": bool(cfg.get("redirect_uri")),
        "redirect_https": bool(cfg.get("redirect_https")),
        "credential_encryption_configured": crypto_ok,
        "api_version": GRAPH_API_VERSION,
        "app_mode": cfg.get("app_mode"),
        "scopes_requested": cfg.get("scopes"),
        "facebook_publishing_enabled": False,
        "instagram_publishing_enabled": False,
        "capabilities": ["facebook_page", "instagram_professional"],
        "oauth_start_path": "/api/v1/social-agent/connections/meta/start",
        "callback_path": "/social-agent/accounts/meta/callback",
        "message": message,
    }


def telegram_adapter_status() -> dict[str, Any]:
    """Narrow read-only status from AutoStory mutation/session safety flags."""
    mutations = (os.environ.get("STORY_MUTATIONS_ENABLED") or "false").strip().lower()
    mode = (os.environ.get("TELEGRAM_SESSION_ENCRYPTION_MODE") or "").strip() or "unknown"
    controlled = (os.environ.get("CONTROLLED_STORY_EXECUTION_ENABLED") or "false").strip().lower()
    return {
        "provider": "telegram",
        "status": "BACKEND_READY",
        "adapter": "AutoStoryPublishingAdapter",
        "session_encryption_mode": mode,
        "story_mutations_enabled": mutations in {"1", "true", "yes", "on"},
        "controlled_story_execution_enabled": controlled in {"1", "true", "yes", "on"},
        "live_publish": "denied_fail_closed",
        "message": "Telegram status via narrow AutoStory adapter; live Story mutations remain fail-closed.",
    }


def exswaping_status() -> dict[str, Any]:
    return {
        "provider": "exswaping",
        "status": "NOT_CONFIGURED",
        "message": "Not configured — approved public-content API required.",
        "blocked_tools": ["exswaping.get_public_content"],
    }


def ai_provider_status() -> dict[str, Any]:
    provider = (os.environ.get("AI_AGENT_PROVIDER") or os.environ.get("SOCIAL_AGENT_AI_PROVIDER") or "").strip()
    configured = bool(provider) and provider.lower() not in {"", "none", "off"}
    return {
        "provider": provider or None,
        "status": "BACKEND_READY" if configured else "CREDENTIALS_MISSING",
        "configured": configured,
        "message": (
            "AI provider configured for assistant tools."
            if configured
            else "AI provider not configured; local draft tools still work without model calls."
        ),
    }


def integration_matrix() -> dict[str, Any]:
    return {
        "meta": meta_status(),
        "telegram": telegram_adapter_status(),
        "x": {"provider": "x", "status": "NOT_STARTED"},
        "linkedin": {"provider": "linkedin", "status": "NOT_STARTED"},
        "discord": {"provider": "discord", "status": "NOT_STARTED"},
        "youtube": {"provider": "youtube", "status": "NOT_STARTED"},
        "tiktok": {"provider": "tiktok", "status": "NOT_STARTED"},
        "exswaping": exswaping_status(),
        "ai": ai_provider_status(),
    }
