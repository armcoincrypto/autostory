"""
P9.5 — Readiness worker v2 (metadata-only; default dry_run=True).

No Telethon connect. Uses manager_v2 for conservative status derivation.
Writes only to account_readiness_snapshots_v2 when dry_run=False.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from sqlalchemy.orm import Session

import structlog

from src.ai_agent.account_allowlist import RESERVED_AI_AGENT_ACCOUNT_IDS
from src.clients.manager_v2 import get_account_metadata
from src.clients.session_resolve import ERR_LEGACY_SQLITE_SESSION_FORMAT
from src.core.account_operational_state import CONTROLLER_ACCOUNT_IDS
from src.core.models import Account
from src.readiness.readiness_proof_chain import (
    REASON_AUTH_OK_NOT_ENABLED,
    evaluate_auth_ok_not_enabled,
    is_stale_v1_legacy_error,
)
from src.readiness.readiness_store_v2 import WriteSnapshotPlan, write_snapshot

logger = structlog.get_logger(__name__)

MAX_BATCH_SIZE = 20
DEFAULT_DRY_RUN = True


class WorkerReadinessStatus(str, Enum):
    READY = "READY"
    AUTH_OK_NOT_ENABLED = "AUTH_OK_NOT_ENABLED"
    RESERVED = "RESERVED"
    CONTROLLER = "CONTROLLER"
    LEGACY_SCHEMA = "LEGACY_SCHEMA"
    ERROR = "ERROR"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ReadinessWorkerResult:
    account_id: int
    status: WorkerReadinessStatus
    failure_code: Optional[str]
    reason: Optional[str]
    tier: Optional[str]
    dry_run: bool
    write_plan: Optional[WriteSnapshotPlan]
    warnings: tuple[str, ...]


def validate_batch_size(account_ids: list[int]) -> list[int]:
    ids = [int(x) for x in account_ids]
    if len(ids) > MAX_BATCH_SIZE:
        raise ValueError(f"batch size {len(ids)} exceeds max {MAX_BATCH_SIZE}")
    if len(ids) == 0:
        raise ValueError("account_ids must not be empty")
    return ids


def derive_readiness_status(meta: dict[str, Any]) -> ReadinessWorkerResult:
    """Map manager_v2 metadata to conservative worker status (no Telethon)."""
    aid = int(meta.get("account_id", 0))
    warnings: list[str] = list(meta.get("warnings") or [])
    if not meta.get("found"):
        return ReadinessWorkerResult(
            account_id=aid,
            status=WorkerReadinessStatus.UNKNOWN,
            failure_code="account_not_in_db",
            reason="account not found",
            tier=None,
            dry_run=True,
            write_plan=None,
            warnings=tuple(warnings),
        )

    tier = (meta.get("tier") or "").strip().lower()
    resolver = (meta.get("resolver_code") or "").strip() or None

    if tier == "reserved" or meta.get("is_reserved"):
        return ReadinessWorkerResult(
            account_id=aid,
            status=WorkerReadinessStatus.RESERVED,
            failure_code=None,
            reason="reserved_account_immutable",
            tier=tier,
            dry_run=True,
            write_plan=None,
            warnings=tuple(warnings),
        )

    if tier == "controller" or meta.get("is_controller"):
        return ReadinessWorkerResult(
            account_id=aid,
            status=WorkerReadinessStatus.CONTROLLER,
            failure_code=None,
            reason="controller_account_operator_critical",
            tier=tier,
            dry_run=True,
            write_plan=None,
            warnings=tuple(warnings),
        )

    if resolver == ERR_LEGACY_SQLITE_SESSION_FORMAT:
        return ReadinessWorkerResult(
            account_id=aid,
            status=WorkerReadinessStatus.LEGACY_SCHEMA,
            failure_code=ERR_LEGACY_SQLITE_SESSION_FORMAT,
            reason="telethon_sqlite_schema_v8_incompatible_with_runtime_1_42",
            tier=tier,
            dry_run=True,
            write_plan=None,
            warnings=tuple(warnings),
        )

    if resolver:
        return ReadinessWorkerResult(
            account_id=aid,
            status=WorkerReadinessStatus.ERROR,
            failure_code=resolver,
            reason=f"resolver_blocks:{resolver}",
            tier=tier,
            dry_run=True,
            write_plan=None,
            warnings=tuple(warnings),
        )

    proof_qual = evaluate_auth_ok_not_enabled(meta)
    if proof_qual is not None:
        return ReadinessWorkerResult(
            account_id=aid,
            status=WorkerReadinessStatus.AUTH_OK_NOT_ENABLED,
            failure_code=None,
            reason=REASON_AUTH_OK_NOT_ENABLED,
            tier=tier,
            dry_run=True,
            write_plan=None,
            warnings=tuple(warnings),
        )

    readiness = (meta.get("readiness_status") or "").strip().upper()
    if readiness == "READY" and meta.get("session_exists"):
        return ReadinessWorkerResult(
            account_id=aid,
            status=WorkerReadinessStatus.READY,
            failure_code=None,
            reason="metadata_ready_no_telethon_verify",
            tier=tier,
            dry_run=True,
            write_plan=None,
            warnings=tuple(warnings),
        )

    if readiness == "ERROR":
        fc = meta.get("readiness_failure_code") or "readiness_error"
        if is_stale_v1_legacy_error(meta):
            return ReadinessWorkerResult(
                account_id=aid,
                status=WorkerReadinessStatus.UNKNOWN,
                failure_code=None,
                reason="schema_v7_awaiting_live_auth_proof",
                tier=tier,
                dry_run=True,
                write_plan=None,
                warnings=tuple(warnings),
            )
        return ReadinessWorkerResult(
            account_id=aid,
            status=WorkerReadinessStatus.ERROR,
            failure_code=str(fc),
            reason="readiness_snapshot_error",
            tier=tier,
            dry_run=True,
            write_plan=None,
            warnings=tuple(warnings),
        )

    return ReadinessWorkerResult(
        account_id=aid,
        status=WorkerReadinessStatus.UNKNOWN,
        failure_code=None,
        reason="insufficient_metadata_conservative_unknown",
        tier=tier,
        dry_run=True,
        write_plan=None,
        warnings=tuple(warnings),
    )


def process_account(
    db: Session,
    account_id: int,
    *,
    dry_run: bool = DEFAULT_DRY_RUN,
) -> ReadinessWorkerResult:
    """Evaluate one account and optionally persist to v2 table."""
    meta = get_account_metadata(db, int(account_id), include_lock=False)
    derived = derive_readiness_status(meta)

    # Reserved/controller never fleet-ready — status already RESERVED/CONTROLLER
    st = derived.status.value
    if int(account_id) in RESERVED_AI_AGENT_ACCOUNT_IDS and st == WorkerReadinessStatus.READY.value:
        st = WorkerReadinessStatus.RESERVED.value
    if int(account_id) in CONTROLLER_ACCOUNT_IDS and st == WorkerReadinessStatus.READY.value:
        st = WorkerReadinessStatus.CONTROLLER.value

    plan = write_snapshot(
        db,
        int(account_id),
        st,
        reason=derived.reason,
        failure_code=derived.failure_code,
        checked_at=datetime.now(timezone.utc),
        dry_run=dry_run,
    )

    return ReadinessWorkerResult(
        account_id=int(account_id),
        status=WorkerReadinessStatus(st),
        failure_code=derived.failure_code,
        reason=derived.reason,
        tier=derived.tier,
        dry_run=dry_run,
        write_plan=plan,
        warnings=derived.warnings,
    )


def run_batch(
    db: Session,
    account_ids: list[int],
    *,
    dry_run: bool = DEFAULT_DRY_RUN,
) -> list[ReadinessWorkerResult]:
    """Process up to MAX_BATCH_SIZE accounts."""
    ids = validate_batch_size(account_ids)
    results: list[ReadinessWorkerResult] = []
    for aid in ids:
        if db.get(Account, aid) is None:
            results.append(
                ReadinessWorkerResult(
                    account_id=aid,
                    status=WorkerReadinessStatus.UNKNOWN,
                    failure_code="account_not_in_db",
                    reason="skipped",
                    tier=None,
                    dry_run=dry_run,
                    write_plan=None,
                    warnings=("account_not_in_db",),
                )
            )
            continue
        results.append(process_account(db, aid, dry_run=dry_run))
    logger.info(
        "readiness_worker_v2_batch_done",
        count=len(results),
        dry_run=dry_run,
        statuses={r.account_id: r.status.value for r in results},
    )
    return results
