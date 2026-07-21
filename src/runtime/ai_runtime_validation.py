"""P10.11 AI Agent / Dexpert runtime validation."""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from src.ai_agent.account_allowlist import RESERVED_AI_AGENT_ACCOUNT_IDS, account_id_permitted_for_ai_agent_tasks
from src.core.ai_agent_models import AiAgentAudit, AiAgentMessage, AiAgentTask
from src.core.models import Account


def validate_ai_runtime(db: Session) -> dict[str, Any]:
    accounts = []
    for aid in sorted(RESERVED_AI_AGENT_ACCOUNT_IDS):
        acc = db.get(Account, int(aid))
        accounts.append(
            {
                "account_id": int(aid),
                "found": acc is not None,
                "permitted": account_id_permitted_for_ai_agent_tasks(db, int(aid)),
                "purpose": (acc.purpose or None) if acc else None,
            }
        )

    active_statuses = ("draft", "active", "awaiting_operator", "running")
    active_tasks = (
        db.query(AiAgentTask)
        .filter(AiAgentTask.status.in_(active_statuses))
        .order_by(AiAgentTask.id.desc())
        .limit(50)
        .all()
    )
    return {
        "track": "E",
        "outcome": "AI_RUNTIME_READY",
        "reserved_accounts": accounts,
        "active_task_count": len(active_tasks),
        "active_tasks": [
            {
                "task_id": int(t.id),
                "account_id": int(t.account_id),
                "status": t.status,
                "auto_mode": t.auto_mode,
                "stage": t.negotiation_stage,
            }
            for t in active_tasks
        ],
        "message_count": db.query(AiAgentMessage).count(),
        "audit_count": db.query(AiAgentAudit).count(),
        "safety_note": "P10.11 validation does not auto-launch AI messaging or autonomous campaigns.",
    }
