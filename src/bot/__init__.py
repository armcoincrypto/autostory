"""
Telegram Bot Dashboard package.

``StoryFleetBot`` is loaded lazily so ``python -m src.bot.kathleen_account_listener``
does not import ``bot.py`` (Telethon dashboard) at interpreter startup.
"""

from __future__ import annotations

__all__ = ("StoryFleetBot",)


def __getattr__(name: str):
    if name == "StoryFleetBot":
        from .bot import StoryFleetBot

        return StoryFleetBot
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
