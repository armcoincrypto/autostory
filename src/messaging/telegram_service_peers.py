"""Telegram service / security peer classification for Owner Messages.

Evidence-based exclusions only. Telegram's official notification peer is 777000
(login codes, 2FA / session security notices). Do not expand without evidence.
"""
from __future__ import annotations

import re
from typing import Any, Optional

# Telegram official service / login-code notification user.
TELEGRAM_SERVICE_USER_IDS = frozenset({777000})

_LOGIN_CODE_BODY_RE = re.compile(
    r"(?i)(?:login\s*code|код\s*для\s*входа|verification\s*code|"
    r"two[\s-]?factor|2fa|код\s*подтверждения)"
)
# Standalone OTP-like tokens in security notices (4–8 digits).
_OTP_TOKEN_RE = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")

SENSITIVE_PEER_ERROR = "SENSITIVE_TELEGRAM_CHAT"
SENSITIVE_PEER_MESSAGE = (
    "This is a Telegram security / login-code chat and cannot be used for Messages."
)


def _digits_only(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def canonical_telegram_user_id(peer: Any) -> Optional[int]:
    """Extract bare Telegram user id when peer is clearly a user id form."""
    if peer is None:
        return None
    if isinstance(peer, int):
        return int(peer) if peer > 0 else None
    s = str(peer).strip()
    if not s:
        return None
    # Strip @ and marked forms; service user is never -100…
    if s.startswith("@"):
        return None
    if s.startswith("-100"):
        return None
    digits = _digits_only(s)
    if not digits:
        return None
    try:
        n = int(digits)
    except ValueError:
        return None
    # Marked channel ids are huge; Telegram service user is small positive.
    if n in TELEGRAM_SERVICE_USER_IDS:
        return n
    return n if 0 < n < 10_000_000_000 else None


def is_telegram_service_peer(peer: Any, *, peer_type: Optional[str] = None) -> bool:
    """True for Telegram service/security notification peers (e.g. 777000)."""
    _ = peer_type  # reserved for future evidenced peer types
    uid = canonical_telegram_user_id(peer)
    return uid is not None and uid in TELEGRAM_SERVICE_USER_IDS


def is_sensitive_telegram_system_chat(
    peer: Any,
    *,
    peer_type: Optional[str] = None,
    title: Optional[str] = None,
    username: Optional[str] = None,
) -> bool:
    """Owner Messages classification for sensitive Telegram system chats."""
    _ = title
    if is_telegram_service_peer(peer, peer_type=peer_type):
        return True
    # Username alone is not enough (other chats can be named Telegram).
    _ = username
    return False


def mask_sensitive_message_text(text: str) -> str:
    """Mask login codes / OTP-like content in Telegram security notices."""
    raw = text if text is not None else ""
    if not raw.strip():
        return raw
    if not (_LOGIN_CODE_BODY_RE.search(raw) or _OTP_TOKEN_RE.search(raw)):
        # Still mask dense numeric OTPs that look like codes even without keywords
        # when message is short (typical login code push).
        if len(raw.strip()) <= 80 and _OTP_TOKEN_RE.search(raw):
            return "Telegram security notification — Sensitive code hidden"
        return raw
    return "Telegram security notification — Sensitive code hidden"


def deny_sensitive_peer_payload(peer: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "error": SENSITIVE_PEER_ERROR,
        "error_code": SENSITIVE_PEER_ERROR,
        "message": SENSITIVE_PEER_MESSAGE,
        "peer": str(peer) if peer is not None else None,
        "sensitive": True,
        "would_send": False,
    }
