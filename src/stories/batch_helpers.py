"""Shared story batch eligibility helpers (P1 minimal restore)."""
from __future__ import annotations

from typing import Any

from config.settings import settings


def get_story_eligible_accounts_for_batch(
    *,
    only_alive: bool = False,
    max_accounts: int = 10,
    max_warming_accounts: int | None = None,
    **kwargs: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Return eligible account dicts and skipped rows for story batch operations.

    P1 restore: conservative empty selection unless callers patch in tests.
    """
    del only_alive, max_warming_accounts, kwargs
    cap = min(int(max_accounts), int(getattr(settings.warmup, "max_accounts_per_story_batch", 10) or 10))
    return [], [{"reason": "p1_minimal_restore_no_selection", "max_accounts": cap}]
