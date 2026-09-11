"""Canonical owner chat-type policy for Messages (Wave M).

Does not hide Telegram limitations. Private/bot Send Now remains the only
certified live send path until MESSAGES_GROUP_CHANNEL_SEND_ENABLED is on.
"""
from __future__ import annotations

from typing import Any

from src.messaging.chat_flags import messages_group_channel_send_enabled

# Canonical peer type labels (UI + API).
CHAT_PRIVATE = "private"
CHAT_BOT = "bot"
CHAT_GROUP = "group"
CHAT_SUPERGROUP = "supergroup"
CHAT_CHANNEL = "channel"

ALL_CHAT_TYPES = frozenset(
    {CHAT_PRIVATE, CHAT_BOT, CHAT_GROUP, CHAT_SUPERGROUP, CHAT_CHANNEL}
)
PRIVATE_LIKE = frozenset({CHAT_PRIVATE, CHAT_BOT, "user"})
GROUP_LIKE = frozenset({CHAT_GROUP, CHAT_SUPERGROUP, CHAT_CHANNEL})


def normalize_chat_type(peer_type: str | None) -> str:
    pt = (peer_type or CHAT_PRIVATE).strip().lower()
    if pt in {"user", "dm"}:
        return CHAT_PRIVATE
    return pt


def chat_capabilities(peer_type: str | None) -> dict[str, bool]:
    """
    Capability matrix for a chat type (policy defaults; send still needs
    write permission + flags at execution time).
    """
    pt = normalize_chat_type(peer_type)
    if pt not in ALL_CHAT_TYPES:
        return {
            "listable": False,
            "history_readable": False,
            "send_now_allowed": False,
            "schedule_allowed": False,
            "join_allowed": False,
            "leave_allowed": False,
        }

    if pt in PRIVATE_LIKE or pt == CHAT_BOT:
        return {
            "listable": True,
            "history_readable": True,
            "send_now_allowed": True,
            "schedule_allowed": True,
            "join_allowed": False,
            "leave_allowed": False,
        }

    # Groups / channels: list + history yes; join/leave yes; send gated by flag.
    group_send = messages_group_channel_send_enabled()
    return {
        "listable": True,
        "history_readable": True,
        "send_now_allowed": group_send,
        "schedule_allowed": group_send,
        "join_allowed": True,
        "leave_allowed": True,
    }


def evaluate_send_policy(
    peer_type: str | None,
    *,
    can_post: bool | None = None,
) -> tuple[bool, str, str]:
    """
    Returns (allowed, code, message) for owner Send Now / schedule.

    Private/bot: allowed by chat type (execution still uses ODMS + flags).
    Group/channel: denied while MESSAGES_GROUP_CHANNEL_SEND_ENABLED is false,
    or when can_post is explicitly False.
    """
    pt = normalize_chat_type(peer_type)
    if pt in PRIVATE_LIKE or pt == CHAT_BOT:
        return True, "OK", "ok"

    if pt not in GROUP_LIKE:
        return False, "PEER_INVALID", f"Unsupported peer type: {pt}."

    if not messages_group_channel_send_enabled():
        return (
            False,
            "GROUP_CHANNEL_SEND_DISABLED",
            "Sending to groups and channels is not enabled yet.",
        )

    if can_post is False:
        return (
            False,
            "NO_WRITE_PERMISSION",
            "This account cannot post in this chat (read-only / broadcast-only).",
        )
    return True, "OK", "ok"


def policy_matrix_for_status() -> dict[str, Any]:
    """Owner-facing capability snapshot for /api/messages/status."""
    out: dict[str, Any] = {}
    for pt in (CHAT_PRIVATE, CHAT_BOT, CHAT_GROUP, CHAT_SUPERGROUP, CHAT_CHANNEL):
        caps = chat_capabilities(pt)
        out[pt] = {
            "LIST": caps["listable"],
            "HISTORY": caps["history_readable"],
            "SEND": caps["send_now_allowed"],
            "SCHEDULE": caps["schedule_allowed"],
            "JOIN": caps["join_allowed"],
            "LEAVE": caps["leave_allowed"],
        }
    return out
