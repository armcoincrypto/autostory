"""
P9.7 — Read-only v2 readiness observer (API + v1/v2 comparison).

No writes. v2 is observational only; v1 remains authoritative for dashboard/scheduler.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session

from src.core.account_operational_state import compute_operational_states
from src.readiness.readiness_store_v2 import (
    ReadinessViewStatus,
    read_snapshot,
    read_snapshot_v2,
)


def _tier_for_account(db: Session, account_id: int) -> tuple[Optional[str], Optional[int], list[str]]:
    """Metadata from operational state (read-only)."""
    rows = compute_operational_states(db, account_ids=[int(account_id)])
    if not rows:
        return None, None, []
    row = rows[0]
    return row.get("tier"), row.get("schema_version"), list(row.get("warnings") or [])


def build_v2_snapshot_row(db: Session, account_id: int) -> dict[str, Any]:
    """
    Single account v2 API payload.

    Missing v2 row → v2_status NOT_FOUND + warning no_v2_snapshot.
    """
    aid = int(account_id)
    tier, schema_version, op_warnings = _tier_for_account(db, aid)
    snap = read_snapshot_v2(db, aid)
    warnings = list(op_warnings)

    if snap.status == ReadinessViewStatus.NOT_FOUND:
        warnings.append("no_v2_snapshot")
        return {
            "account_id": aid,
            "v2_status": ReadinessViewStatus.NOT_FOUND.value,
            "v2_reason": None,
            "failure_code": None,
            "checked_at": None,
            "tier": tier,
            "schema_version": schema_version,
            "warnings": warnings,
        }

    if snap.status == ReadinessViewStatus.TABLE_MISSING:
        warnings.append("v2_table_missing")
        return {
            "account_id": aid,
            "v2_status": ReadinessViewStatus.TABLE_MISSING.value,
            "v2_reason": snap.reason,
            "failure_code": snap.failure_code,
            "checked_at": snap.checked_at,
            "tier": tier,
            "schema_version": schema_version,
            "warnings": warnings,
        }

    warnings.extend(snap.warnings)
    return {
        "account_id": aid,
        "v2_status": snap.status.value,
        "v2_reason": snap.reason,
        "failure_code": snap.failure_code,
        "checked_at": snap.checked_at,
        "tier": tier,
        "schema_version": schema_version,
        "warnings": warnings,
    }


def build_v2_snapshots(
    db: Session,
    *,
    account_ids: Optional[list[int]] = None,
) -> list[dict[str, Any]]:
    if account_ids is None:
        from src.readiness.readiness_models_v2 import AccountReadinessSnapshotV2

        account_ids = [
            int(r[0])
            for r in db.query(AccountReadinessSnapshotV2.account_id)
            .order_by(AccountReadinessSnapshotV2.account_id)
            .all()
        ]
    return [build_v2_snapshot_row(db, aid) for aid in account_ids]


def explain_v1_v2_mismatch(
    *,
    v1_status: Optional[str],
    v2_status: Optional[str],
    v1_failure_code: Optional[str],
    tier: Optional[str],
) -> str:
    """Classify v1 vs v2 divergence for operators (read-only)."""
    s1 = (v1_status or "").strip().upper() or "NOT_FOUND"
    s2 = (v2_status or "").strip().upper() or "NOT_FOUND"

    if s2 == "NOT_FOUND":
        return "no_v2_snapshot"
    if s1 == s2:
        return "match"
    if s1 == "ERROR" and s2 == "LEGACY_SCHEMA":
        return "acceptable_legacy_semantics"
    if s1 == "READY" and s2 == "RESERVED" and (tier or "").lower() == "reserved":
        return "intentional_tier_reserved"
    if s1 == "READY" and s2 == "CONTROLLER" and (tier or "").lower() == "controller":
        return "intentional_tier_controller"
    if s1 == "ERROR" and s2 == "ERROR":
        return "match"
    fc = (v1_failure_code or "").strip()
    if s1 == "ERROR" and s2 == "ERROR" and fc:
        return "match"
    return "status_mismatch"


def compare_readiness_v1_v2(
    db: Session,
    account_ids: list[int],
) -> list[dict[str, Any]]:
    """Side-by-side v1 vs v2 for observer CLI/API (no writes)."""
    out: list[dict[str, Any]] = []
    tier_by_id: dict[int, Optional[str]] = {}
    for row in compute_operational_states(db, account_ids=account_ids):
        tier_by_id[int(row["account_id"])] = row.get("tier")

    for aid in account_ids:
        v1 = read_snapshot(db, int(aid))
        v2 = read_snapshot_v2(db, int(aid))
        tier = tier_by_id.get(int(aid))
        v1_st = (
            v1.status.value
            if v1.status != ReadinessViewStatus.NOT_FOUND
            else "NOT_FOUND"
        )
        v2_st = v2.status.value
        mismatch = explain_v1_v2_mismatch(
            v1_status=v1_st,
            v2_status=v2_st,
            v1_failure_code=v1.failure_code,
            tier=tier,
        )
        out.append(
            {
                "account_id": int(aid),
                "v1_status": v1_st,
                "v1_checked_at": v1.checked_at,
                "v2_status": v2_st,
                "v2_checked_at": v2.checked_at,
                "mismatch_reason": mismatch,
                "tier": tier,
            }
        )
    return out
