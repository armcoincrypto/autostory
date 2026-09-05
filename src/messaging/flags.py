"""Messages execution kill switch (independent of scheduler mutations)."""
from __future__ import annotations

import os

from config.settings import settings


def messages_execution_enabled() -> bool:
    """Fail-closed: owner DM live send requires explicit enablement."""
    env = (os.environ.get("MESSAGES_EXECUTION_ENABLED") or "").strip().lower()
    if env:
        return env in {"1", "true", "yes", "on"}
    return bool(getattr(settings, "messages_execution_enabled", False))
