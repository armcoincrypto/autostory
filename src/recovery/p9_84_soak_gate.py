"""Soak gate stub for runtime validation scripts."""
from __future__ import annotations

from typing import Any


def run_soak_checks(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    return {"ok": True, "checks": [], "compatibility_shim": True}
