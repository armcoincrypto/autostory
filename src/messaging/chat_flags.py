"""Owner Messages chat join/leave/group-send kill switches (Wave M).

Independent of SCHEDULER_MUTATIONS_ENABLED and DISCOVERY_EXECUTION_ENABLED.
Default fail-closed.
"""
from __future__ import annotations

import os

from config.settings import settings


def _env_bool(name: str, settings_attr: str) -> bool:
    env = (os.environ.get(name) or "").strip().lower()
    if env:
        return env in {"1", "true", "yes", "on"}
    return bool(getattr(settings, settings_attr, False))


def messages_chat_join_enabled() -> bool:
    """Fail-closed: owner Messages join requires MESSAGES_CHAT_JOIN_ENABLED."""
    return _env_bool("MESSAGES_CHAT_JOIN_ENABLED", "messages_chat_join_enabled")


def messages_chat_leave_enabled() -> bool:
    """Fail-closed: owner Messages leave requires MESSAGES_CHAT_LEAVE_ENABLED."""
    return _env_bool("MESSAGES_CHAT_LEAVE_ENABLED", "messages_chat_leave_enabled")


def messages_group_channel_send_enabled() -> bool:
    """Fail-closed: group/channel Send Now / schedule until explicitly certified."""
    return _env_bool(
        "MESSAGES_GROUP_CHANNEL_SEND_ENABLED",
        "messages_group_channel_send_enabled",
    )
