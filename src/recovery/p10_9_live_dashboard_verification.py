"""Module eligibility stub for accounts-v2."""
from __future__ import annotations

from typing import Any


def module_eligibility_for_account(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    return {
        "eligible": False,
        "reasons": ["compatibility_shim"],
        "compatibility_shim": True,
    }
