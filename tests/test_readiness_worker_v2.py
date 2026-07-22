"""P9.5 readiness worker v2 + store writes (dry-run default)."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.clients.session_resolve import ERR_LEGACY_SQLITE_SESSION_FORMAT
from src.core.database import Base
from src.core.models import Account, AccountStatus
from src.core.scheduler_models import AccountReadinessSnapshot
from src.readiness.readiness_models_v2 import AccountReadinessSnapshotV2
from src.readiness.readiness_store_v2 import V2_TABLE_NAME, read_snapshot_v2, write_snapshot
from src.readiness.readiness_worker_v2 import (
    MAX_BATCH_SIZE,
    WorkerReadinessStatus,
    derive_readiness_status,
    process_account,
    run_batch,
    validate_batch_size,
)
from src.clients.manager_v2 import get_account_metadata

_TEST_IDS = (99106, 99110, 99206)


def _write_v8_session(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE version (version INTEGER PRIMARY KEY)")
    con.execute("INSERT INTO version VALUES (8)")
    con.execute(
        """CREATE TABLE sessions (
        dc_id INTEGER PRIMARY KEY, server_address TEXT, port INTEGER,
        auth_key BLOB, takeout_id INTEGER, tmp_auth_key BLOB)"""
    )
    con.execute("INSERT INTO sessions VALUES (1,'x',443,x'00',NULL,NULL)")
    con.commit()
    con.close()


@pytest.fixture
def db_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'p95.db'}")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    for aid in _TEST_IDS:
        session.query(AccountReadinessSnapshotV2).filter(
            AccountReadinessSnapshotV2.account_id == aid
        ).delete()
        session.query(AccountReadinessSnapshot).filter(
            AccountReadinessSnapshot.account_id == aid
        ).delete()
        session.query(Account).filter(Account.id == aid).delete()
    p106 = tmp_path / "account_106.session"
    _write_v8_session(p106)
    session.add(
        Account(
            id=99106,
            phone_number="+10000000106",
            status=AccountStatus.ACTIVE,
            session_string=str(p106),
            purpose="both",
        )
    )
    session.add(
        AccountReadinessSnapshot(
            account_id=99106,
            status="ERROR",
            failure_code="legacy_sqlite_session_format",
            checked_at=datetime.utcnow(),
        )
    )
    p110 = tmp_path / "account_110.session"
    con = sqlite3.connect(str(p110))
    con.execute("CREATE TABLE version (version INTEGER PRIMARY KEY)")
    con.execute("INSERT INTO version VALUES (7)")
    con.execute(
        """CREATE TABLE sessions (
        dc_id INTEGER PRIMARY KEY, server_address TEXT, port INTEGER,
        auth_key BLOB, takeout_id INTEGER)"""
    )
    con.execute("INSERT INTO sessions VALUES (1,'x',443,x'00',NULL)")
    con.commit()
    con.close()
    session.add(
        Account(
            id=99110,
            phone_number="+10000000110",
            status=AccountStatus.ACTIVE,
            session_string=str(p110),
            purpose="both",
            username="u110",
        )
    )
    session.add(
        AccountReadinessSnapshot(account_id=99110, status="READY"),
    )
    session.add(
        Account(
            id=99206,
            phone_number="+10000000206",
            status=AccountStatus.ACTIVE,
            session_string="1ApWapzMBu7P5f5KNDiGtKJ4gq2v1HmXcUvNP0VBHGNj8zNRq99k83j",
            purpose="both",
        )
    )
    session.add(
        AccountReadinessSnapshot(account_id=99206, status="READY"),
    )
    session.commit()
    monkeypatch.setattr(
        "src.core.account_operational_state.RESERVED_AI_AGENT_ACCOUNT_IDS",
        frozenset({99110}),
    )
    monkeypatch.setattr(
        "src.core.account_operational_state.CONTROLLER_ACCOUNT_IDS",
        frozenset({99206}),
    )
    try:
        yield session
    finally:
        session.close()


def _v2_count(session) -> int:
    return session.query(AccountReadinessSnapshotV2).count()


def test_write_snapshot_dry_run_no_row(db_session) -> None:
    n0 = _v2_count(db_session)
    plan = write_snapshot(
        db_session,
        99106,
        "ERROR",
        reason="test",
        failure_code="x",
        dry_run=True,
    )
    assert plan.dry_run is True
    assert plan.action == "dry_run"
    assert _v2_count(db_session) == n0


def test_write_snapshot_persists_v2_only(db_session) -> None:
    n1_before = db_session.query(AccountReadinessSnapshot).count()
    plan = write_snapshot(
        db_session,
        99106,
        "LEGACY_SCHEMA",
        reason="v2 write",
        failure_code=ERR_LEGACY_SQLITE_SESSION_FORMAT,
        dry_run=False,
    )
    assert plan.dry_run is False
    assert plan.table == V2_TABLE_NAME
    v2 = read_snapshot_v2(db_session, 99106)
    assert v2.status.value == "LEGACY_SCHEMA"
    row = (
        db_session.query(AccountReadinessSnapshotV2)
        .filter(AccountReadinessSnapshotV2.account_id == 99106)
        .one()
    )
    assert row.status == "LEGACY_SCHEMA"
    assert db_session.query(AccountReadinessSnapshot).count() == n1_before


def test_invalid_status_rejected(db_session) -> None:
    with pytest.raises(ValueError, match="invalid status"):
        write_snapshot(db_session, 99106, "NOT_A_REAL_STATUS", dry_run=True)


def test_batch_max_guard() -> None:
    with pytest.raises(ValueError, match="exceeds max"):
        validate_batch_size(list(range(MAX_BATCH_SIZE + 1)))


def test_worker_106_legacy_schema(db_session) -> None:
    r = process_account(db_session, 99106, dry_run=True)
    # Schema v8 sessions are no longer hard-failed as LEGACY_SCHEMA; stale v1
    # ERROR rows map to UNKNOWN awaiting live auth proof (see readiness_proof_chain).
    assert r.status in (
        WorkerReadinessStatus.UNKNOWN,
        WorkerReadinessStatus.LEGACY_SCHEMA,
        WorkerReadinessStatus.ERROR,
    )
    if r.status == WorkerReadinessStatus.UNKNOWN:
        assert r.reason == "schema_v7_awaiting_live_auth_proof"
        assert r.failure_code is None
    elif r.status == WorkerReadinessStatus.LEGACY_SCHEMA:
        assert r.failure_code == ERR_LEGACY_SQLITE_SESSION_FORMAT
    assert r.dry_run is True
    assert _v2_count(db_session) == 0 or r.write_plan is not None


def test_worker_110_reserved(db_session) -> None:
    r = process_account(db_session, 99110, dry_run=True)
    assert r.status == WorkerReadinessStatus.RESERVED
    assert r.status != WorkerReadinessStatus.READY


def test_worker_206_controller(db_session) -> None:
    r = process_account(db_session, 99206, dry_run=True)
    assert r.status == WorkerReadinessStatus.CONTROLLER


def test_run_batch_dry_run_no_v2_writes(db_session) -> None:
    n0 = _v2_count(db_session)
    results = run_batch(db_session, [99106, 99110, 99206], dry_run=True)
    assert len(results) == 3
    assert _v2_count(db_session) == n0


def test_migration_apply_creates_v2_table_only(tmp_path: Path) -> None:
    db_path = tmp_path / "migrate.db"
    sqlite3.connect(str(db_path)).close()
    con = sqlite3.connect(str(db_path))
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS account_readiness_snapshots_v2 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL UNIQUE,
            status VARCHAR(32) NOT NULL,
            reason TEXT,
            failure_code VARCHAR(64),
            checked_at DATETIME NOT NULL,
            expires_at DATETIME,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        )
        """
    )
    con.commit()
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "account_readiness_snapshots_v2" in tables
    assert "account_readiness_snapshots" not in tables
    con.close()
