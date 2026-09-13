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


def canonical_peer_key(peer_id: str | None, peer_type: str | None = None) -> str:
    """Stable identity for catalog dedupe (merges bare and -100 marked forms)."""
    raw = (peer_id or "").strip()
    if not raw:
        return ""
    pt = (peer_type or "").strip().lower()
    if pt in {"user", "dm"}:
        pt = "private"
    if raw.startswith("@"):
        return f"user:{raw.lstrip('@').lower()}"
    if raw.startswith("-100") and raw[4:].isdigit():
        return f"channel:{raw[4:]}"
    if raw.startswith("-") and raw[1:].isdigit():
        # Ambiguous without type: basic group vs already-marked.
        if pt in {"channel", "supergroup"}:
            return f"channel:{raw[1:]}"
        return f"group:{raw[1:]}"
    if raw.isdigit():
        if pt in {"channel", "supergroup"}:
            return f"channel:{raw}"
        if pt == "group":
            return f"group:{raw}"
        if pt in {"private", "bot"}:
            return f"user:{raw}"
        # Unknown type + bare positive: prefer channel merge (common dialog id form).
        return f"id:{raw}"
    return f"raw:{raw.lower()}"


def marked_peer_id_from_entity(entity_id: int | str | None, chat_type: str | None) -> str | None:
    """Mark a Telethon entity id for API/UI use (same rules as normalize_peer_target)."""
    if entity_id is None:
        return None
    return normalize_peer_target(str(entity_id), chat_type)


def preferred_display_peer_id(peer_id: str | None, peer_type: str | None = None) -> str:
    """Owner-facing peer id: prefer marked form for groups/channels."""
    raw = (peer_id or "").strip()
    if not raw:
        return ""
    return normalize_peer_target(raw, peer_type) if raw.isdigit() or (
        raw.startswith("-") and raw[1:].isdigit()
    ) or (raw.startswith("-100") and raw[4:].isdigit()) else raw
