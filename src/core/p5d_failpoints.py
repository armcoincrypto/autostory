"""P5D certification failpoints — disabled unless P5D_CERTIFICATION_MODE is active."""
from __future__ import annotations

import os
from typing import Optional


class P5DFailpointStop(Exception):
    """Deterministic worker stop for P5D interruption testing; leaves last DB state committed."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(name)


def p5d_certification_mode() -> bool:
    return os.environ.get("P5D_CERTIFICATION_MODE", "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def p5d_active_failpoint() -> Optional[str]:
    if not p5d_certification_mode():
        return None
    raw = (os.environ.get("P5D_FAILPOINT") or "").strip()
    return raw or None


def p5d_hit_failpoint(name: str) -> None:
    """Raise P5DFailpointStop when the named failpoint is armed in certification mode."""
    if p5d_active_failpoint() == name:
        raise P5DFailpointStop(name)
