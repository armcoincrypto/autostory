"""Owner direct-message messaging foundation (Wave 6A).

Policy-free Telegram primitives live here. Product policy (AI Agent allowlist,
owner Messages eligibility, kill switches) belongs in callers / OwnerDirectMessageService.
"""
from __future__ import annotations

from src.messaging.eligibility import evaluate_dm_account_eligibility
from src.messaging.flags import messages_execution_enabled
from src.messaging.owner_dm_service import OwnerDirectMessageService

__all__ = [
    "OwnerDirectMessageService",
    "evaluate_dm_account_eligibility",
    "messages_execution_enabled",
]
