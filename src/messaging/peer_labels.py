"""Owner-facing peer labels for Messages lists (no Telegram calls)."""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session

from src.core.models import Account
from src.core.scheduler_models import ChatTarget
from src.messaging.peer_ids import normalize_peer_target


def _entity_numeric(peer_id: str) -> Optional[int]:
    raw = (peer_id or "").strip()
    if not raw or raw.startswith("@"):
        return None
    if raw.startswith("-100") and raw[4:].isdigit():
        return int(raw[4:])
    if raw.startswith("-") and raw[1:].isdigit():
        return int(raw[1:])
    if raw.isdigit():
        return int(raw)
    return None


def resolve_owner_peer_label(
    db: Session,
    peer_id: Optional[str],
    peer_type: Optional[str] = None,
    *,
    peer_username: Optional[str] = None,
) -> str:
    """Best-effort friendly chat label for owner tables (DB only)."""
    uname = (peer_username or "").strip().lstrip("@")
    if uname:
        return f"@{uname}"
    raw = (peer_id or "").strip()
    if not raw:
        return "Chat"
    if raw.startswith("@"):
        return raw
    pt = (peer_type or "").strip().lower()
    if pt in {"user", "dm"}:
        pt = "private"

    entity_id = _entity_numeric(raw)
    if entity_id is not None:
        # Private: match fleet account telegram user_id
        if pt in {"private", "bot", ""} or not pt:
            acc = db.query(Account).filter(Account.user_id == int(entity_id)).first()
            if acc:
                if acc.first_name:
                    return str(acc.first_name)
                if acc.username:
                    return f"@{acc.username}"
                return f"Account #{acc.id}"

        # Group/channel: match chat_targets by tg_id (bare or marked)
        candidates = {int(entity_id)}
        marked = normalize_peer_target(str(entity_id), pt or "supergroup")
        mnum = _entity_numeric(marked)
        if mnum is not None:
            candidates.add(mnum)
        if raw.lstrip("-").isdigit() or (raw.startswith("-100") and raw[4:].isdigit()):
            try:
                candidates.add(int(raw))
            except ValueError:
                pass
        target = (
            db.query(ChatTarget)
            .filter(ChatTarget.tg_id.in_(list(candidates)))
            .order_by(ChatTarget.id.desc())
            .first()
        )
        if target and (target.title or target.username):
            return str(target.title or f"@{target.username}")

    if pt == "channel":
        return "Channel"
    if pt in {"group", "supergroup"}:
        return "Group chat"
    if pt == "bot":
        return "Bot"
    return "Private chat"


def owner_safe_error_copy(error_code: Optional[str], error_message: Optional[str] = None) -> str:
    """Map internal failure codes to owner-safe short copy."""
    code = (error_code or "").strip().upper()
    mapping = {
        "UNKNOWN": "Temporary Telegram error",
        "PEER_INVALID": "Chat could not be resolved",
        "NO_WRITE_PERMISSION": "Cannot post in this chat",
        "GROUP_CHANNEL_SEND_DISABLED": "Sending to groups and channels is not enabled",
        "MESSAGES_DISABLED": "Message sending is currently disabled",
        "ACCOUNT_PROTECTED": "Account unavailable",
        "ACCOUNT_RESERVED": "Account unavailable",
        "ACCOUNT_DISABLED": "Account unavailable",
        "ACCOUNT_INELIGIBLE": "Account unavailable",
        "AUTH_REQUIRED": "Account needs login",
        "RATE_LIMITED": "Rate limit reached — try later",
        "FLOOD_WAIT": "Telegram rate limit — try later",
        "PEER_FLOOD": "Telegram rate limit — try later",
        "EMPTY_MESSAGE": "Message is empty",
        "MESSAGE_TOO_LONG": "Message is too long",
        "UNCERTAIN": "Delivery uncertain — check chat before retrying",
        "USERNAME_NOT_FOUND": "Chat could not be found",
        "HISTORY_UNAVAILABLE": "Unable to load conversation history",
        "AI_DRAFT_PROVIDER_ERROR": "AI Draft is temporarily unavailable. You can still write and send manually.",
        "AI_DRAFT_DISABLED": "AI Draft is currently unavailable. You can still write and send manually.",
        "AI_DRAFT_TIMEOUT": "AI Draft is temporarily unavailable. You can still write and send manually.",
    }
    if code in mapping:
        return mapping[code]
    msg = (error_message or "").strip()
    # Strip Telethon class-name style noise
    if "Error" in msg and ":" in msg and len(msg) < 120:
        return "Temporary Telegram error"
    if msg and len(msg) <= 160 and "Request" not in msg and "access_hash" not in msg.lower():
        return msg
    return "Could not complete this action"


def owner_unavailable_label(eligibility_code: Optional[str]) -> str:
    code = (eligibility_code or "").strip().upper()
    return {
        "PROTECTED": "Protected",
        "RESERVED": "Reserved",
        "DISABLED": "Disabled",
        "ACCOUNT_DISABLED": "Disabled",
        "AUTH_FAILED": "Needs login",
        "AUTH_REQUIRED": "Needs login",
        "NEEDS_SESSION": "Needs login",
        "INELIGIBLE": "Unavailable",
    }.get(code, "Unavailable")


def account_bucket(eligible: bool, eligibility_code: Optional[str]) -> str:
    """available | attention | unavailable"""
    if eligible:
        return "available"
    code = (eligibility_code or "").strip().upper()
    if code in {"AUTH_FAILED", "AUTH_REQUIRED", "NEEDS_SESSION"}:
        return "attention"
    return "unavailable"
