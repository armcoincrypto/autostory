"""
Template renderer - variables like {account_name}, {date}, {random_emoji}
"""
import random
from datetime import datetime
from typing import Optional, Dict, Any

EMOJIS = "🚀📢✨💡🔥👍⭐🎯📣💪🌟📝🔔🎉"


def render_template(
    body: str,
    account_name: Optional[str] = None,
    chat_title: Optional[str] = None,
    cta_link: Optional[str] = None,
    timezone: str = "Asia/Yerevan",
) -> str:
    """Replace template variables with actual values"""
    now = datetime.utcnow()
    context = {
        "account_name": account_name or "Account",
        "chat_title": chat_title or "Chat",
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "random_emoji": random.choice(EMOJIS),
        "cta_link": cta_link or "",
    }
    result = body
    for key, value in context.items():
        result = result.replace("{" + key + "}", str(value))
    return result
