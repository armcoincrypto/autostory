"""Governance change audit log (additive, append-only)."""
from __future__ import annotations

import json
from typing import Any

from src.core.database import get_db_context
from src.governance.models import AccountGovernanceAuditLog


def log_governance_action(
    *,
    account_id: int | None,
    action: str,
    target_type: str,
    target_value: str,
    reason: str | None = None,
    actor: str | None = None,
    payload: dict[str, Any] | None = None,
) -> int:
    with get_db_context() as db:
        row = AccountGovernanceAuditLog(
            account_id=account_id,
            action=action,
            target_type=target_type,
            target_value=target_value,
            reason=(reason or "").strip() or None,
            actor=(actor or "").strip() or None,
            payload_json=json.dumps(payload or {}, default=str),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return int(row.id)
