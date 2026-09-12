"""Normalize Telegram peer ids for Telethon entity resolution.

Dialogs / ChatInvite expose positive channel/supergroup ids. Telethon treats a
bare positive int as PeerUser, so group/channel sends must use Bot API marked
ids (``-100…`` for channels/supergroups, ``-…`` for basic groups).
"""
from __future__ import annotations


def normalize_peer_target(target: str, peer_type: str | None = None) -> str:
    """Return a Telethon-resolvable peer string for the given chat type."""
    raw = (target or "").strip()
    if not raw:
        return raw
    pt = (peer_type or "").strip().lower()
    if pt in {"user", "dm"}:
        pt = "private"
    if raw.startswith("@"):
        return raw
    # Already marked / signed — keep as-is.
    if raw.startswith("-") and raw[1:].isdigit():
        return raw
    if not raw.isdigit():
        return raw
    if pt in {"channel", "supergroup"}:
        return f"-100{raw}"
    if pt == "group":
        return f"-{raw}"
    return raw


def marked_peer_id_from_entity(entity_id: int | str | None, chat_type: str | None) -> str | None:
    """Mark a Telethon entity id for API/UI use (same rules as normalize_peer_target)."""
    if entity_id is None:
        return None
    return normalize_peer_target(str(entity_id), chat_type)
