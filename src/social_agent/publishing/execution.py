"""Publishing execution modes and global Meta gates."""
from __future__ import annotations

import os
from enum import Enum


class PublishingExecutionMode(str, Enum):
    DISABLED = "disabled"
    DRY_RUN = "dry-run"
    CONTROLLED_CANARY = "controlled-canary"
    LIVE = "live"


# Exact canary destination — hard-coded for this certification phase.
CANARY_FACEBOOK_PAGE_ID = "867560236439580"
CANARY_FACEBOOK_PAGE_NAME = "Exswaping"
CANARY_MESSAGE = "Social Agent connection test — no customer action required."
CANARY_DESTINATION = "facebook_page"
CANARY_AUTH_TTL_SECONDS = 300
CANARY_REQUIRED_PERMISSION = "pages_manage_posts"


def _truthy(name: str, default: str = "false") -> bool:
    return (os.environ.get(name) or default).strip().lower() in {"1", "true", "yes", "on"}


def facebook_publishing_enabled() -> bool:
    return _truthy("META_FACEBOOK_PUBLISHING_ENABLED", "false")


def instagram_publishing_enabled() -> bool:
    return _truthy("META_INSTAGRAM_PUBLISHING_ENABLED", "false")


def publishing_execution_mode() -> PublishingExecutionMode:
    raw = (os.environ.get("META_PUBLISHING_EXECUTION_MODE") or "disabled").strip().lower()
    for mode in PublishingExecutionMode:
        if mode.value == raw:
            return mode
    return PublishingExecutionMode.DISABLED


def assert_instagram_hard_disabled() -> dict:
    if instagram_publishing_enabled():
        return {
            "ok": False,
            "error": "INSTAGRAM_PUBLISHING_MUST_STAY_DISABLED",
            "message": "Instagram publishing remains hard-disabled for this phase.",
        }
    return {"ok": True}
