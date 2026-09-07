"""Owner Messages AI draft kill switch (independent of MESSAGES_EXECUTION_ENABLED)."""
from __future__ import annotations

import os

from config.settings import settings


def messages_ai_draft_enabled() -> bool:
    """True when AI drafting is allowed. Env: MESSAGES_AI_DRAFT_ENABLED."""
    env = (os.environ.get("MESSAGES_AI_DRAFT_ENABLED") or "").strip().lower()
    if env in {"1", "true", "yes", "on"}:
        return True
    if env in {"0", "false", "no", "off"}:
        return False
    return bool(getattr(settings, "messages_ai_draft_enabled", False))


def openai_api_key_configured() -> bool:
    """True when OPENAI_API_KEY is present (canonical shared secret owner)."""
    key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if key:
        return True
    return bool((getattr(settings, "openai_api_key", None) or "").strip())
