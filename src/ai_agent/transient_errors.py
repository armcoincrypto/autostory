"""
Classify Telegram / transport errors that should not fail the AI Agent task or draft.

Used by approve_send, sync_inbound, and auto_loop so the desk waits and retries instead
of marking drafts failed or tasks broken when the session lock or network is busy.
"""
from __future__ import annotations

from typing import Any, Optional

# Exact codes from ClientManager, Telethon _map_error, or Telethon exception names.
_TRANSIENT_CODES_EXACT = frozenset(
    {
        "session_lock_timeout",
        "session_db_locked",
        "gateway_timeout",
        "gateway_error",
        "database_busy",
        "FloodWait",
        "PeerFlood",
        "SlowModeWait",
        "TimeoutError",
        "asyncio.TimeoutError",
        "ConnectionError",
        "ConnectionResetError",
        "OSError",
    }
)


def is_transient_telegram_error(
    error_code: Optional[str],
    error_message: Optional[str] = None,
) -> bool:
    """
    Return True when the operator should retry later without changing task outcome.

    Treats session lock contention, rate limits, FloodWait-family, and common
    timeout / connection issues as transient. Conservative when code is empty.
    """
    c_raw = (error_code or "").strip()
    m = (error_message or "").strip().lower()

    if c_raw in _TRANSIENT_CODES_EXACT:
        return True
    c_low = c_raw.lower()
    for x in _TRANSIENT_CODES_EXACT:
        if c_low == x.lower():
            return True

    # Alternate spellings / manager strings
    if c_low in (
        "floodwait",
        "peerflood",
        "slowmodewait",
        "timeout",
        "timed_out",
        "timeouterror",
        "connectionerror",
        "connection_reset",
        "connectionabortederror",
    ):
        return True

    if "session_lock" in c_low or "session_db_locked" in c_low:
        return True
    if "flood" in c_low and "wait" in c_low:
        return True
    if "slowmode" in c_low or "slow_mode" in c_low:
        return True

    if "timeout" in c_low or "timed out" in c_low:
        return True
    if "connection" in c_low or "network" in c_low:
        return True

    if m:
        if "session lock" in m or "exclusive session lock" in m:
            return True
        if "timeout" in m or "timed out" in m:
            return True
        if "connection" in m or "network" in m:
            return True
        if "flood" in m and "wait" in m:
            return True

    return False


def is_transient_message_meta(meta_json: Any) -> bool:
    """True if a stored message meta indicates a prior transient send failure."""
    if not isinstance(meta_json, dict):
        return False
    if meta_json.get("approve_send_transient"):
        return True
    err = meta_json.get("approve_send_error")
    if isinstance(err, dict):
        return is_transient_telegram_error(
            str(err.get("error_code") or ""),
            str(err.get("error_message") or ""),
        )
    return False
