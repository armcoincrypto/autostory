"""Thin negotiation intel helpers (dashboard/tests)."""
from __future__ import annotations

from typing import Any


def negotiation_rate_bucket(facts: dict[str, Any] | None) -> str | None:
    if not facts:
        return None
    return facts.get("rate_evaluation")
