"""
P9.4+ — Readiness snapshot access (v1 read-only; v2 table writes behind dry_run guard).

Never writes to legacy ``account_readiness_snapshots``.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Optional, Union

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

import structlog

logger = structlog.get_logger(__name__)

V2_TABLE_NAME = "account_readiness_snapshots_v2"

ALLOWED_WRITE_STATUSES = frozenset(
    {
        "READY",
        "AUTH_OK_NOT_ENABLED",
        "ERROR",
        "TEMP_CONNECT",
        "NOT_AUTHORIZED",
        "RESERVED",
        "CONTROLLER",
        "LEGACY_SCHEMA",
        "UNKNOWN",
    }
)


class ReadinessViewStatus(str, Enum):
    """Conservative status for v2 readers."""

    READY = "READY"
    AUTH_OK_NOT_ENABLED = "AUTH_OK_NOT_ENABLED"
    ERROR = "ERROR"
    TEMP_CONNECT = "TEMP_CONNECT"
    NOT_AUTHORIZED = "NOT_AUTHORIZED"
    RESERVED = "RESERVED"
    CONTROLLER = "CONTROLLER"
    LEGACY_SCHEMA = "LEGACY_SCHEMA"
    UNKNOWN = "UNKNOWN"
    NOT_FOUND = "NOT_FOUND"
    TABLE_MISSING = "TABLE_MISSING"


@dataclass(frozen=True)
class ReadinessSnapshotView:
    account_id: int
    status: ReadinessViewStatus
    failure_code: Optional[str]
    reason: Optional[str]
    checked_at: Optional[str]
    expires_at: Optional[str]
    source: str
    warnings: tuple[str, ...]


def _iso(dt: Any) -> Optional[str]:
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(dt)


def _map_status(raw: Optional[str]) -> ReadinessViewStatus:
    s = (raw or "").strip().upper()
    if not s:
        return ReadinessViewStatus.UNKNOWN
    for member in (
        ReadinessViewStatus.READY,
        ReadinessViewStatus.AUTH_OK_NOT_ENABLED,
        ReadinessViewStatus.ERROR,
        ReadinessViewStatus.TEMP_CONNECT,
        ReadinessViewStatus.NOT_AUTHORIZED,
        ReadinessViewStatus.RESERVED,
        ReadinessViewStatus.CONTROLLER,
        ReadinessViewStatus.LEGACY_SCHEMA,
        ReadinessViewStatus.UNKNOWN,
    ):
        if s == member.value:
            return member
    return ReadinessViewStatus.UNKNOWN


def _table_exists_orm(db: Session, table_name: str) -> bool:
    try:
        bind = db.get_bind()
        if bind is None:
            return False
        return table_name in sa_inspect(bind).get_table_names()
    except Exception:
        return False


def read_snapshot_orm(db: Session, account_id: int) -> ReadinessSnapshotView:
    """Read snapshot via SQLAlchemy (read-only query)."""
    aid = int(account_id)
    warnings: list[str] = []
    if not _table_exists_orm(db, "account_readiness_snapshots"):
        warnings.append("table_missing")
        return ReadinessSnapshotView(
            account_id=aid,
            status=ReadinessViewStatus.TABLE_MISSING,
            failure_code=None,
            reason=None,
            checked_at=None,
            expires_at=None,
            source="orm",
            warnings=tuple(warnings),
        )
    from src.core.scheduler_models import AccountReadinessSnapshot

    snap = (
        db.query(AccountReadinessSnapshot)
        .filter(AccountReadinessSnapshot.account_id == aid)
        .first()
    )
    if snap is None:
        return ReadinessSnapshotView(
            account_id=aid,
            status=ReadinessViewStatus.NOT_FOUND,
            failure_code=None,
            reason=None,
            checked_at=None,
            expires_at=None,
            source="orm",
            warnings=tuple(warnings),
        )
    status = _map_status(snap.status)
    return ReadinessSnapshotView(
        account_id=aid,
        status=status,
        failure_code=(snap.failure_code or "").strip() or None,
        reason=(snap.reason or "").strip() or None,
        checked_at=_iso(snap.checked_at),
        expires_at=_iso(snap.expires_at),
        source="orm",
        warnings=tuple(warnings),
    )


def read_snapshot_sqlite(
    db_path: Union[str, Path],
    account_id: int,
) -> ReadinessSnapshotView:
    """Read snapshot via read-only SQLite URI (no ORM)."""
    aid = int(account_id)
    path = Path(db_path).expanduser().resolve()
    warnings: list[str] = []
    if not path.is_file():
        warnings.append("db_file_missing")
        return ReadinessSnapshotView(
            account_id=aid,
            status=ReadinessViewStatus.UNKNOWN,
            failure_code=None,
            reason=None,
            checked_at=None,
            expires_at=None,
            source="sqlite_ro",
            warnings=tuple(warnings),
        )
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as e:
        warnings.append(f"sqlite_open_failed:{type(e).__name__}")
        return ReadinessSnapshotView(
            account_id=aid,
            status=ReadinessViewStatus.ERROR,
            failure_code="sqlite_open_failed",
            reason=str(e),
            checked_at=None,
            expires_at=None,
            source="sqlite_ro",
            warnings=tuple(warnings),
        )
    try:
        tables = {
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "account_readiness_snapshots" not in tables:
            warnings.append("table_missing")
            return ReadinessSnapshotView(
                account_id=aid,
                status=ReadinessViewStatus.TABLE_MISSING,
                failure_code=None,
                reason=None,
                checked_at=None,
                expires_at=None,
                source="sqlite_ro",
                warnings=tuple(warnings),
            )
        row = con.execute(
            """
            SELECT status, failure_code, reason, checked_at, expires_at
            FROM account_readiness_snapshots
            WHERE account_id = ?
            LIMIT 1
            """,
            (aid,),
        ).fetchone()
        if row is None:
            return ReadinessSnapshotView(
                account_id=aid,
                status=ReadinessViewStatus.NOT_FOUND,
                failure_code=None,
                reason=None,
                checked_at=None,
                expires_at=None,
                source="sqlite_ro",
                warnings=tuple(warnings),
            )
        status = _map_status(row[0])
        return ReadinessSnapshotView(
            account_id=aid,
            status=status,
            failure_code=(row[1] or "").strip() or None,
            reason=(row[2] or "").strip() or None,
            checked_at=_iso(row[3]),
            expires_at=_iso(row[4]),
            source="sqlite_ro",
            warnings=tuple(warnings),
        )
    except sqlite3.Error as e:
        warnings.append(f"sqlite_read_failed:{type(e).__name__}")
        return ReadinessSnapshotView(
            account_id=aid,
            status=ReadinessViewStatus.ERROR,
            failure_code="sqlite_read_failed",
            reason=str(e),
            checked_at=None,
            expires_at=None,
            source="sqlite_ro",
            warnings=tuple(warnings),
        )
    finally:
        con.close()


def read_snapshot(
    db_or_path: Union[Session, str, Path],
    account_id: int,
) -> ReadinessSnapshotView:
    """Dispatch to ORM or raw SQLite reader (legacy v1 table)."""
    if isinstance(db_or_path, Session):
        return read_snapshot_orm(db_or_path, account_id)
    return read_snapshot_sqlite(db_or_path, account_id)


@dataclass(frozen=True)
class WriteSnapshotPlan:
    account_id: int
    status: str
    reason: Optional[str]
    failure_code: Optional[str]
    checked_at: str
    expires_at: Optional[str]
    table: str
    action: Literal["dry_run", "insert", "update"]
    dry_run: bool


def _parse_checked_at(checked_at: Optional[datetime | str]) -> datetime:
    if checked_at is None:
        return datetime.now(timezone.utc).replace(tzinfo=None)
    if isinstance(checked_at, datetime):
        return checked_at.replace(tzinfo=None) if checked_at.tzinfo else checked_at
    s = str(checked_at).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _validate_write_payload(
    account_id: int,
    status: str,
    reason: Optional[str],
    failure_code: Optional[str],
) -> str:
    aid = int(account_id)
    if aid <= 0:
        raise ValueError("account_id must be positive")
    st = (status or "").strip().upper()
    if st not in ALLOWED_WRITE_STATUSES:
        raise ValueError(f"invalid status: {status!r}")
    return st


def write_snapshot(
    db: Session,
    account_id: int,
    status: str,
    *,
    reason: Optional[str] = None,
    failure_code: Optional[str] = None,
    checked_at: Optional[datetime | str] = None,
    expires_at: Optional[datetime | str] = None,
    dry_run: bool = True,
) -> WriteSnapshotPlan:
    """
    Write readiness to **v2 table only**. Requires ``dry_run=False`` to persist.

    Default ``dry_run=True`` returns a planned action without DB mutation.
    """
    st = _validate_write_payload(account_id, status, reason, failure_code)
    aid = int(account_id)
    checked = _parse_checked_at(checked_at)
    checked_iso = checked.isoformat() + "Z"
    exp_dt = _parse_checked_at(expires_at) if expires_at is not None else None
    exp_iso = exp_dt.isoformat() + "Z" if exp_dt else None

    if not _table_exists_orm(db, V2_TABLE_NAME):
        if dry_run:
            return WriteSnapshotPlan(
                account_id=aid,
                status=st,
                reason=reason,
                failure_code=failure_code,
                checked_at=checked_iso,
                expires_at=exp_iso,
                table=V2_TABLE_NAME,
                action="dry_run",
                dry_run=True,
            )
        raise RuntimeError(
            f"{V2_TABLE_NAME} missing — run scripts/ops/p95_prepare_readiness_v2_table.py --apply"
        )

    from src.readiness.readiness_models_v2 import AccountReadinessSnapshotV2

    existing = (
        db.query(AccountReadinessSnapshotV2)
        .filter(AccountReadinessSnapshotV2.account_id == aid)
        .first()
    )
    action: Literal["dry_run", "insert", "update"] = "update" if existing else "insert"

    plan = WriteSnapshotPlan(
        account_id=aid,
        status=st,
        reason=reason,
        failure_code=failure_code,
        checked_at=checked_iso,
        expires_at=exp_iso,
        table=V2_TABLE_NAME,
        action="dry_run" if dry_run else action,
        dry_run=dry_run,
    )

    if dry_run:
        logger.info("readiness_store_v2_write_dry_run", **plan.__dict__)
        return plan

    now = datetime.utcnow()
    if existing:
        existing.status = st
        existing.reason = reason
        existing.failure_code = failure_code
        existing.checked_at = checked
        existing.expires_at = exp_dt
        existing.updated_at = now
    else:
        row = AccountReadinessSnapshotV2(
            account_id=aid,
            status=st,
            reason=reason,
            failure_code=failure_code,
            checked_at=checked,
            expires_at=exp_dt,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
    db.commit()
    logger.info("readiness_store_v2_write_applied", account_id=aid, status=st, action=action)
    return plan


def read_snapshot_v2(db: Session, account_id: int) -> ReadinessSnapshotView:
    """Read from v2 table only (read-only)."""
    aid = int(account_id)
    warnings: list[str] = []
    if not _table_exists_orm(db, V2_TABLE_NAME):
        warnings.append("v2_table_missing")
        return ReadinessSnapshotView(
            account_id=aid,
            status=ReadinessViewStatus.TABLE_MISSING,
            failure_code=None,
            reason=None,
            checked_at=None,
            expires_at=None,
            source="orm_v2",
            warnings=tuple(warnings),
        )
    from src.readiness.readiness_models_v2 import AccountReadinessSnapshotV2

    snap = (
        db.query(AccountReadinessSnapshotV2)
        .filter(AccountReadinessSnapshotV2.account_id == aid)
        .first()
    )
    if snap is None:
        return ReadinessSnapshotView(
            account_id=aid,
            status=ReadinessViewStatus.NOT_FOUND,
            failure_code=None,
            reason=None,
            checked_at=None,
            expires_at=None,
            source="orm_v2",
            warnings=tuple(warnings),
        )
    return ReadinessSnapshotView(
        account_id=aid,
        status=_map_status(snap.status),
        failure_code=(snap.failure_code or "").strip() or None,
        reason=(snap.reason or "").strip() or None,
        checked_at=_iso(snap.checked_at),
        expires_at=_iso(snap.expires_at),
        source="orm_v2",
        warnings=tuple(warnings),
    )
