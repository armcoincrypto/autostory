"""Pilot-only prohibited pilot account ids."""
from __future__ import annotations

from src.core.account_protection import PROTECTED_IDS

PROHIBITED_PILOT_IDS = frozenset(PROTECTED_IDS)


def filter_prohibited(ids):
    return frozenset(i for i in ids if int(i) not in PROHIBITED_PILOT_IDS)
