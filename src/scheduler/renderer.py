"""
Template renderer - variables like {account_name}, {date}, {random_emoji}
"""
import random
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from zoneinfo import ZoneInfo

from src.scheduler.timezone import DEFAULT_OWNER_TIMEZONE

EMOJIS = "🚀📢✨💡🔥👍⭐🎯📣💪🌟📝🔔🎉"


def render_template(
    body: str,
    account_name: Optional[str] = None,
    chat_title: Optional[str] = None,
    cta_link: Optional[str] = None,
    timezone: str = DEFAULT_OWNER_TIMEZONE,
) -> str:
    """Replace template variables with actual values (date/time in profile TZ)."""
    try:
        tz = ZoneInfo((timezone or DEFAULT_OWNER_TIMEZONE).strip() or DEFAULT_OWNER_TIMEZONE)
    except Exception:
        tz = ZoneInfo(DEFAULT_OWNER_TIMEZONE)
    now = datetime.now(timezone.utc).astimezone(tz)
    context = {
        "account_name": account_name or "Account",
        "chat_title": chat_title or "Chat",
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "random_emoji": random.choice(EMOJIS),
        "cta_link": cta_link or "",
    }
    out = body
    for key, value in context.items():
        out = out.replace("{" + key + "}", str(value))
    return out
