"""No-op freshness enricher for scheduler readiness rows."""
from __future__ import annotations

from typing import Any


def enrich_scheduler_readiness_row_p9_34(_db: Any, row: dict[str, Any]) -> dict[str, Any]:
    row.setdefault("freshness_shim", True)
    return row
