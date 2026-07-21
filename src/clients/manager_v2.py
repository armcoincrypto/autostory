"""
P9.4 — Account/session metadata facade (additive; no Telethon in this phase).

Combines operational-state core logic with optional lock inspection.
Not imported by production services until a later cutover phase.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session

from src.core.account_operational_state import (
    CONTROLLER_ACCOUNT_IDS,
    compute_account_operational_state,
    compute_operational_states,
)
from src.core.models import Account
from src.core.session_lock_v2 import LockInspection, inspect_lock
from src.readiness.readiness_store_v2 import ReadinessSnapshotView, read_snapshot


def get_account_metadata(
    db: Session,
    account_id: int,
    *,
    include_lock: bool = True,
    locks_dir: Optional[str] = None,
) -> dict[str, Any]:
    """
    Read-only account foundation view for operators/automation.

    Uses :func:`compute_account_operational_state` (no Telethon connect).
  """
    acct = db.get(Account, int(account_id))
    if acct is None:
        return {
            "account_id": int(account_id),
            "found": False,
            "tier": None,
            "warnings": ["account_not_in_db"],
        }
    row = compute_account_operational_state(db, acct)
    readiness = read_snapshot(db, int(account_id))
    out: dict[str, Any] = {
        "found": True,
        "account_id": int(account_id),
        "tier": row.get("tier"),
        "label": row.get("label"),
        "schema_version": row.get("schema_version"),
        "resolver_code": row.get("resolver_code"),
        "readiness_status": row.get("readiness_status"),
        "readiness_snapshot_status": readiness.status.value,
        "readiness_failure_code": readiness.failure_code,
        "health_status": row.get("health_status"),
        "scheduler_eligible": row.get("scheduler_eligible"),
        "discovery_eligible": row.get("discovery_eligible"),
        "session_path": row.get("session_path"),
        "session_exists": row.get("session_exists"),
        "is_reserved": row.get("tier") == "reserved",
        "is_controller": row.get("tier") == "controller",
        "controller_ids": sorted(CONTROLLER_ACCOUNT_IDS),
        "warnings": list(row.get("warnings") or []),
    }
    if readiness.warnings:
        out["warnings"] = out["warnings"] + list(readiness.warnings)
    if include_lock:
        lock = inspect_lock(int(account_id), locks_dir=locks_dir)
        out["session_lock"] = _lock_to_dict(lock)
    return out


def list_accounts_metadata(
    db: Session,
    account_ids: list[int],
    *,
    include_lock: bool = False,
) -> list[dict[str, Any]]:
    """Batch metadata for specific account ids (order preserved)."""
    return [
        get_account_metadata(db, aid, include_lock=include_lock)
        for aid in account_ids
    ]


def summarize_fleet_slice(
    db: Session,
    account_ids: list[int],
) -> dict[str, Any]:
    """
    Lightweight summary for fleet slices (e.g. 106 / 110 / 206).

    Reuses operational-state rows without Telethon.
    """
    rows = compute_operational_states(db, account_ids=account_ids)
    by_tier: dict[str, int] = {}
    for r in rows:
        t = r.get("tier") or "unknown"
        by_tier[t] = by_tier.get(t, 0) + 1
    return {
        "count": len(rows),
        "by_tier": by_tier,
        "accounts": rows,
    }


def _lock_to_dict(lock: LockInspection) -> dict[str, Any]:
    return {
        "exists": lock.exists,
        "held": lock.held,
        "stale": lock.stale,
        "holder_pid": lock.holder_pid,
        "holder_host": lock.holder_host,
        "acquired_at": lock.acquired_at,
        "lock_path": lock.lock_path,
        "warnings": list(lock.warnings),
    }
