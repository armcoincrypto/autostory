"""Hardcoded governance fallback — always active; never weakened by DB edits."""
from __future__ import annotations

from src.ai_agent.account_allowlist import RESERVED_AI_AGENT_ACCOUNT_IDS
from src.core.account_protection import PROTECTED_IDS, PURPOSE_HOLD_IDS
from src.core.account_operational_state import CONTROLLER_ACCOUNT_IDS
from src.governance.constants import AI_RESERVED, MANUAL_ONLY, SYSTEM_PROTECTED


def fallback_roles_for_account(account_id: int) -> set[str]:
    """Roles implied by legacy hardcoded safety lists (cannot be removed via DB)."""
    aid = int(account_id)
    roles: set[str] = set()
    if aid in PROTECTED_IDS or aid in CONTROLLER_ACCOUNT_IDS:
        roles.add(SYSTEM_PROTECTED)
    if aid in PURPOSE_HOLD_IDS:
        roles.add(MANUAL_ONLY)
    if aid in RESERVED_AI_AGENT_ACCOUNT_IDS:
        roles.add(AI_RESERVED)
    return roles


def fallback_blocked_reasons(account_id: int) -> list[str]:
    aid = int(account_id)
    reasons: list[str] = []
    if aid in PROTECTED_IDS:
        reasons.append("fallback:protected_account_id")
    if aid in CONTROLLER_ACCOUNT_IDS:
        reasons.append("fallback:controller_account")
    if aid in PURPOSE_HOLD_IDS:
        reasons.append("fallback:held_account")
    if aid in RESERVED_AI_AGENT_ACCOUNT_IDS:
        reasons.append("fallback:ai_agent_reserved")
    return reasons
