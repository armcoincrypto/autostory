"""Read-only fleet recovery summary stub."""
from __future__ import annotations

from typing import Any


def build_fleet_recovery_summary(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    return {
        "total": 0,
        "by_status": {},
        "compatibility_shim": True,
    }
