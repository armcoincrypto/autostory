# P9.38 draft restore — source-backed from Cursor snapshots (not byte-matched to archive .pyc).
# Do not restart scheduler until import probe + tests pass and operator approves.

"""
P8.10.4 — Scheduler runtime gate aligned with canonical account_state + session lock probe.

Read-only classification for ``execute_job`` (no sends). Uses the same
``compute_account_operational_state`` lens as Dexpert / dashboard for
``scheduler_message`` eligibility.
"""
from __future__ import annotations

from typing import Any, Literal

from sqlalchemy.orm import Session

from src.core.account_state import (
    BLOCK_SESSION_LOCK_HELD,
    compute_account_operational_state,
    operator_label_for_block_code,
)
from src.core.models import Account
from src.core.scheduler_models import ScheduledJob
from src.core.session_lock import inspect_session_lock

RuntimeGateAction = Literal["proceed", "defer_temp_lock", "skip_permanent"]


def classify_scheduler_runtime_gate(db: Session, account: Account) -> dict[str, Any]:
    """
    Decide whether a scheduled job may proceed for this account *right now*.

    Returns a dict:
      - ``action``: ``proceed`` | ``defer_temp_lock`` | ``skip_permanent``
      - ``primary_blocker``: optional machine code (primary_blocking_reason)
      - ``primary_label``: operator label
      - ``lifecycle_state``: canonical lifecycle string
      - ``permanent_blockers`` / ``temporary_blockers``: lists of codes
    """
    state = compute_account_operational_state(db, account, purpose=None)
    if state.scheduler_eligible:
        return {
            "action": "proceed",
            "primary_blocker": None,
            "primary_label": "Eligible",
            "lifecycle_state": state.lifecycle_state,
            "permanent_blockers": list(state.permanent_blockers),
            "temporary_blockers": list(state.temporary_blockers),
        }

    perm = list(state.permanent_blockers)
    temp = list(state.temporary_blockers)
    primary = state.primary_blocking_reason

    # Only transient blockers (session lock, readiness refresh, temp connect, …)
    if temp and not perm:
        return {
            "action": "defer_temp_lock",
            "primary_blocker": primary,
            "primary_label": operator_label_for_block_code(primary) if primary else "Temporary blocker",
            "lifecycle_state": state.lifecycle_state,
            "permanent_blockers": perm,
            "temporary_blockers": temp,
            "lock_only": bool(temp) and all(
                (c or "").split(":", 1)[0] == BLOCK_SESSION_LOCK_HELD for c in temp
            ),
        }

    return {
        "action": "skip_permanent",
        "primary_blocker": primary,
        "primary_label": operator_label_for_block_code(primary) if primary else "Not eligible",
        "lifecycle_state": state.lifecycle_state,
        "permanent_blockers": perm,
        "temporary_blockers": temp,
    }


def dry_run_scheduler_job_runtime_gate(db: Session, job_id: int) -> dict[str, Any]:
    """
    Read-only evaluation for a single scheduled job (no Telegram, no mutations).

    Returns classification + ``error`` if job/account missing.
    """
    jid = int(job_id)
    job = db.query(ScheduledJob).filter(ScheduledJob.id == jid).first()
    if not job:
        return {"job_id": jid, "error": "job_not_found", "action": "skip_permanent"}
    account = db.query(Account).filter(Account.id == int(job.account_id)).first()
    if not account:
        return {"job_id": jid, "error": "account_not_found", "action": "skip_permanent"}
    gate = classify_scheduler_runtime_gate(db, account)
    lock = inspect_session_lock(int(account.id))
    out = {
        "job_id": jid,
        "account_id": int(account.id),
        **gate,
        "session_lock_held": bool(lock.held),
        "session_lock_subsystem": lock.subsystem,
        "session_lock_stale": bool(lock.stale),
    }
    return out
