"""Parse owner-pasted Telegram chat refs for Messages join/leave/resolve."""
from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import urlparse

from src.clients.joiner import _extract_invite_hash
from src.clients.target_health import (
    HEALTH_INVALID,
    HEALTH_JOINABLE,
    _INVITE_LINK_RE,
    _field_looks_like_private_message_link,
    _field_looks_like_public_message_link,
    _username_looks_valid_public,
)

_TME_USER_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?t\.me/([A-Za-z][A-Za-z0-9_]{3,31})/?$",
    re.IGNORECASE,
)


def parse_chat_ref(raw: str) -> dict[str, Any]:
    """
    Normalize owner paste into username and/or invite fields.

    Returns:
        ok, kind (username|invite|invalid), username, invite_link, display, reason
    """
    s = (raw or "").strip()
    if not s:
        return {
            "ok": False,
            "kind": "invalid",
            "username": None,
            "invite_link": None,
            "display": "",
            "reason": "Paste a @username, t.me link, or invite.",
            "health": HEALTH_INVALID,
        }

    if _field_looks_like_private_message_link(s) or _field_looks_like_public_message_link(s):
        return {
            "ok": False,
            "kind": "invalid",
            "username": None,
            "invite_link": None,
            "display": s,
            "reason": "Message permalinks are not join targets. Use @username or an invite link.",
            "health": HEALTH_INVALID,
        }

    invite_hash = _extract_invite_hash(s)
    if invite_hash or _INVITE_LINK_RE.search(s):
        link = s if "t.me" in s.lower() or s.startswith("+") else s
        if invite_hash and "t.me" not in link.lower() and not link.startswith("+"):
            link = f"https://t.me/+{invite_hash}"
        elif invite_hash and link.startswith("+"):
            link = f"https://t.me/{link}"
        return {
            "ok": True,
            "kind": "invite",
            "username": None,
            "invite_link": link,
            "invite_hash": invite_hash or _extract_invite_hash(link),
            "display": link,
            "reason": "Private invite link",
            "health": HEALTH_JOINABLE,
        }

    m = _TME_USER_RE.match(s)
    if m:
        uname = m.group(1)
        return {
            "ok": True,
            "kind": "username",
            "username": uname,
            "invite_link": None,
            "invite_hash": None,
            "display": f"@{uname}",
            "reason": "Public @username",
            "health": HEALTH_JOINABLE,
        }

    bare = s.lstrip("@").strip()
    # Reject URLs we didn't classify
    if "://" in s or "t.me/" in s.lower():
        try:
            path = urlparse(s if "://" in s else "https://" + s).path or ""
            part = path.strip("/").split("/")[0]
            if part and _username_looks_valid_public(part):
                return {
                    "ok": True,
                    "kind": "username",
                    "username": part.lstrip("@"),
                    "invite_link": None,
                    "invite_hash": None,
                    "display": f"@{part.lstrip('@')}",
                    "reason": "Public @username",
                    "health": HEALTH_JOINABLE,
                }
        except Exception:
            pass
        return {
            "ok": False,
            "kind": "invalid",
            "username": None,
            "invite_link": None,
            "display": s,
            "reason": "Unrecognized Telegram link.",
            "health": HEALTH_INVALID,
        }

    if _username_looks_valid_public(bare):
        return {
            "ok": True,
            "kind": "username",
            "username": bare,
            "invite_link": None,
            "invite_hash": None,
            "display": f"@{bare}",
            "reason": "Public @username",
            "health": HEALTH_JOINABLE,
        }

    return {
        "ok": False,
        "kind": "invalid",
        "username": None,
        "invite_link": None,
        "display": s,
        "reason": "Invalid username or invite.",
        "health": HEALTH_INVALID,
    }


def parsed_as_classify_row(parsed: dict[str, Any]) -> dict[str, Optional[str]]:
    """Shape compatible with classify_target / entity probe helpers."""
    return {
        "username": parsed.get("username"),
        "invite_link": parsed.get("invite_link"),
        "tg_id": None,
        "chat_type": None,
    }
