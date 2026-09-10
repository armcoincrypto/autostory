"""Broadcast execution guard (Wave K).

Broadcast proxies to an external AI Factory campaign engine (:8015) whose
Telegram send path is NOT the certified Storyfleet OwnerDirectMessageService /
gateway path. Until Broadcast is rebuilt on Storyfleet primitives and certified,
execution stays fail-closed by default.
"""
from __future__ import annotations

import os


def broadcast_execution_enabled() -> bool:
    """Canonical Broadcast live-execution flag. Default false (fail-closed)."""
    raw = (os.environ.get("BROADCAST_EXECUTION_ENABLED") or "false").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def broadcast_fail_closed_payload(*, detail: str | None = None) -> dict:
    return {
        "ok": False,
        "error": "broadcast_execution_disabled",
        "execution_enabled": False,
        "message": (
            detail
            or "Broadcast execution is fail-closed. Live bulk sends are not enabled in production."
        ),
        "product": {
            "messages": "one private conversation reply",
            "broadcast": "intentional bulk communication (not enabled)",
        },
    }
