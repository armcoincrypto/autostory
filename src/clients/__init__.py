"""Telegram client management module."""
from __future__ import annotations

from .rate_limiter import RateLimiter
from .session import SessionManager

try:
    from .manager import ClientManager
except ModuleNotFoundError:
    ClientManager = None  # type: ignore[misc, assignment]

__all__ = ["ClientManager", "SessionManager", "RateLimiter"]
