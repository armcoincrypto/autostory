"""Integration status helpers (honest, no fake connected providers)."""
from __future__ import annotations

import os
from typing import Any


def meta_status() -> dict[str, Any]:
    app_id = (os.environ.get("META_APP_ID") or "").strip()
    secret = (os.environ.get("META_APP_SECRET") or "").strip()
    redirect = (os.environ.get("META_REDIRECT_URI") or "").strip()
    configured = bool(app_id and secret and redirect)
    return {
        "provider": "meta",
        "status": "CREDENTIALS_MISSING" if not configured else "BACKEND_READY",
        "configured": configured,
        "app_id_present": bool(app_id),
        "redirect_uri_present": bool(redirect),
        # never return secret
        "capabilities": ["facebook_page", "instagram_professional"],
        "oauth_start_path": "/api/v1/social-agent/meta/oauth/start",
        "message": (
            "Meta OAuth scaffolding ready; configure Meta app ID, app secret, and redirect URI via server environment."
            if not configured
            else "Meta credentials present; complete OAuth connection in Social Accounts."
        ),
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
    # Reuse existing AI agent provider envs when present; do not invent keys.
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
