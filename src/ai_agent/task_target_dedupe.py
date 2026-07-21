"""
One active AI Agent task per normalized Telegram target (cross-account).

Uses ORM column ``AiAgentTask.target_username_or_id`` (see ``AiAgentTask`` model).
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from src.core.ai_agent_models import AiAgentTask

# Business-active: duplicate target blocks new task creation while any of these is open.
AI_AGENT_ACTIVE_STATUSES = frozenset(
    {
        "draft",
        "waiting_admin_approval",
        "waiting_reply",
        "ready_for_operator",
        "risky",
    }
)

# Paused / terminal / anything else — not blocking for duplicate enforcement.
AI_AGENT_INACTIVE_STATUSES = frozenset({"paused", "completed", "failed", "cancelled"})

_DIGITS_ONLY = re.compile(r"^-?\d+$")


def normalize_ai_target(value: Any) -> str:
    """
    Canonical key for matching Telegram targets across tasks.

    - strip / lowercase
    - numeric IDs: normalized integer string (e.g. "00123" -> "123")
    - usernames: leading @ optional, stored as "@handle" lowercase
    - empty string if invalid / empty input
    """
    if value is None:
        return ""
    raw = str(value).strip()
    if not raw or "\x00" in raw:
        return ""
    s = raw.lower().strip()
    if not s:
        return ""
    if _DIGITS_ONLY.fullmatch(s):
        try:
            return str(int(s))
        except ValueError:
            return ""
    if s.startswith("@"):
        inner = s[1:].strip()
        if not inner or _DIGITS_ONLY.fullmatch(inner):
            if inner and _DIGITS_ONLY.fullmatch(inner):
                try:
                    return str(int(inner))
                except ValueError:
                    return ""
            return ""
        return "@" + inner
    return "@" + s


def is_ai_agent_task_active(task: AiAgentTask) -> bool:
    return (task.status or "").strip().lower() in AI_AGENT_ACTIVE_STATUSES


def _task_is_strictly_newer_than(a: AiAgentTask, b: AiAgentTask) -> bool:
    ca, cb = a.created_at or datetime.min, b.created_at or datetime.min
    if ca > cb:
        return True
    if ca < cb:
        return False
    return int(a.id) > int(b.id)


def find_active_task_id_for_normalized_target(
    db: Session,
    normalized: str,
    *,
    exclude_task_id: Optional[int] = None,
) -> Optional[int]:
    """First matching active task id, or None."""
    if not normalized:
        return None
    q = (
        db.query(AiAgentTask)
        .filter(AiAgentTask.status.in_(AI_AGENT_ACTIVE_STATUSES))
        .order_by(AiAgentTask.id.asc())
    )
    if exclude_task_id is not None:
        q = q.filter(AiAgentTask.id != int(exclude_task_id))
    for row in q.all():
        if normalize_ai_target(row.target_username_or_id) == normalized:
            return int(row.id)
    return None


def find_newer_active_duplicate(db: Session, task: AiAgentTask) -> Optional[AiAgentTask]:
    """
    If any *other* active task exists for the same normalized target and is strictly
    newer than ``task``, return the newest such task. Otherwise None.
    """
    norm = normalize_ai_target(task.target_username_or_id)
    if not norm:
        return None
    best: Optional[AiAgentTask] = None
    for o in (
        db.query(AiAgentTask)
        .filter(AiAgentTask.id != int(task.id))
        .filter(AiAgentTask.status.in_(AI_AGENT_ACTIVE_STATUSES))
        .all()
    ):
        if normalize_ai_target(o.target_username_or_id) != norm:
            continue
        if not _task_is_strictly_newer_than(o, task):
            continue
        if best is None or _task_is_strictly_newer_than(o, best):
            best = o
    return best


def ai_agent_tasks_target_column_name() -> str:
    """ORM column name for the Telegram target field (for ops scripts)."""
    return "target_username_or_id"
