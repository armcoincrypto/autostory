"""Claude draft feature flag (independent of Messages send kill switch)."""
from __future__ import annotations

import os

from config.settings import settings


def claude_draft_enabled() -> bool:
    """Fail-closed: Draft with Claude requires explicit enablement."""
    env = (os.environ.get("CLAUDE_DRAFT_ENABLED") or "").strip().lower()
    if env:
        return env in {"1", "true", "yes", "on"}
    return bool(getattr(settings, "claude_draft_enabled", False))


def anthropic_api_key_configured() -> bool:
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if key:
        return True
    return bool((getattr(settings, "anthropic_api_key", None) or "").strip())
