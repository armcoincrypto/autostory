"""Canonical no-touch account sets shared by runtime safety gates.

Keep these constants dependency-free so Story and execution guards do not
import campaign recovery/audit modules merely to classify an account.
"""
from __future__ import annotations

PURPOSE_HOLD_IDS = frozenset({34, 36, 101})
PROTECTED_IDS = frozenset({110, 206, 207})

