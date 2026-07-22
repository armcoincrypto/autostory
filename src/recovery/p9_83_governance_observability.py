"""Governance observability constants and read-only stubs."""
from __future__ import annotations

from typing import Any

from src.core.account_protection import PROTECTED_IDS, PURPOSE_HOLD_IDS

__all__ = [
    "PROTECTED_IDS",
    "PURPOSE_HOLD_IDS",
    "build_lock_snapshot",
    "build_hash_manifest",
]


def build_lock_snapshot(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    return {
        "scheduler_mutations_enabled": False,
        "campaign_execution_enabled": False,
        "compatibility_shim": True,
    }


def build_hash_manifest(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    return {"compatibility_shim": True, "files": []}
