"""Owner-safe DM error classification."""
from __future__ import annotations

from typing import Any, Tuple

from src.scheduler.executor import _map_error as _scheduler_map_error

# Canonical owner-facing codes (uppercase)
OWNER_ERROR_CODES = frozenset(
    {
        "FLOOD_WAIT",
        "PEER_FLOOD",
        "USER_PRIVACY_RESTRICTED",
        "CHAT_WRITE_FORBIDDEN",
        "USER_BLOCKED",
        "USERNAME_NOT_FOUND",
        "PEER_INVALID",
        "AUTH_REQUIRED",
        "SESSION_REVOKED",
        "NETWORK_TIMEOUT",
        "EMPTY_MESSAGE",
        "MESSAGE_TOO_LONG",
        "VALIDATION_ERROR",
        "MESSAGES_DISABLED",
        "ACCOUNT_INELIGIBLE",
        "RATE_LIMITED",
        "UNKNOWN",
    }
)


def _normalize_code(raw: str) -> str:
    key = (raw or "").strip()
    mapping = {
        "FloodWait": "FLOOD_WAIT",
        "PeerFlood": "PEER_FLOOD",
        "ChatWriteForbidden": "CHAT_WRITE_FORBIDDEN",
        "UserPrivacyRestricted": "USER_PRIVACY_RESTRICTED",
        "UserPrivacyRestrictedError": "USER_PRIVACY_RESTRICTED",
        "UsernameNotOccupied": "USERNAME_NOT_FOUND",
        "UsernameNotOccupiedError": "USERNAME_NOT_FOUND",
        "UsernameInvalid": "PEER_INVALID",
        "UsernameInvalidError": "PEER_INVALID",
        "PeerIdInvalid": "PEER_INVALID",
        "PeerIdInvalidError": "PEER_INVALID",
        "UserIsBlocked": "USER_BLOCKED",
        "UserIsBlockedError": "USER_BLOCKED",
        "AuthKeyUnregistered": "SESSION_REVOKED",
        "AuthKeyUnregisteredError": "SESSION_REVOKED",
        "SessionRevoked": "SESSION_REVOKED",
        "SessionPasswordNeeded": "AUTH_REQUIRED",
        "UnauthorizedError": "AUTH_REQUIRED",
        "TimedOut": "NETWORK_TIMEOUT",
        "TimeoutError": "NETWORK_TIMEOUT",
    }
    if key in mapping:
        return mapping[key]
    upper = key.upper()
    if upper in OWNER_ERROR_CODES:
        return upper
    # snake / mixed
    compact = upper.replace("ERROR", "").replace(" ", "_")
    if "FLOOD" in compact and "PEER" in compact:
        return "PEER_FLOOD"
    if "FLOOD" in compact:
        return "FLOOD_WAIT"
    if "PRIVACY" in compact:
        return "USER_PRIVACY_RESTRICTED"
    if "USERNAME" in compact and ("NOT" in compact or "OCCUPIED" in compact):
        return "USERNAME_NOT_FOUND"
    if "BLOCK" in compact:
        return "USER_BLOCKED"
    if "AUTH" in compact or "UNAUTHORIZED" in compact:
        return "AUTH_REQUIRED"
    if "TIMEOUT" in compact or "TIMED_OUT" in compact:
        return "NETWORK_TIMEOUT"
    return "UNKNOWN"


def map_dm_error(exc: Exception) -> Tuple[str, str, dict[str, Any]]:
    """Return (owner_code, owner_message, extras)."""
    name = type(exc).__name__
    extras: dict[str, Any] = {}
    try:
        code, msg = _scheduler_map_error(exc)
    except Exception:
        code, msg = name, str(exc)[:240]
    owner = _normalize_code(code) if code else _normalize_code(name)
    if owner == "UNKNOWN":
        owner = _normalize_code(name)
    if owner == "FLOOD_WAIT":
        seconds = getattr(exc, "seconds", None)
        if seconds is not None:
            extras["retry_after"] = int(seconds)
            msg = f"Temporarily rate limited by Telegram — wait {int(seconds)}s."
        else:
            msg = "Temporarily rate limited by Telegram."
    elif owner == "USER_PRIVACY_RESTRICTED":
        msg = "This user only accepts messages from contacts."
    elif owner == "USERNAME_NOT_FOUND":
        msg = "Username not found."
    elif owner == "PEER_INVALID":
        msg = "Invalid recipient."
    elif owner == "USER_BLOCKED":
        msg = "Cannot message this user (blocked)."
    elif owner == "AUTH_REQUIRED":
        msg = "Telegram session requires re-authentication."
    elif owner == "SESSION_REVOKED":
        msg = "Telegram session was revoked."
    elif owner == "NETWORK_TIMEOUT":
        msg = "Telegram network timeout."
    return owner, (msg or str(exc))[:240], extras
