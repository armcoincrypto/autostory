"""Minimal recovery-lab helpers used by campaign governance gates."""
from __future__ import annotations

from typing import Optional

from src.ai_agent.account_allowlist import RESERVED_AI_AGENT_ACCOUNT_IDS
from src.core.account_operational_state import CONTROLLER_ACCOUNT_IDS


def is_prohibited_account(account_id: int) -> tuple[bool, Optional[str]]:
    aid = int(account_id)
    if aid in RESERVED_AI_AGENT_ACCOUNT_IDS:
        return True, "reserved_ai_agent_account"
    if aid in CONTROLLER_ACCOUNT_IDS:
        return True, "controller_account"
    return False, None
