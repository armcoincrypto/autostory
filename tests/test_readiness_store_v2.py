"""P9.4 readiness_store_v2 — temp DB tests."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.database import Base
from src.core.models import Account, AccountStatus
from src.core.scheduler_models import AccountReadinessSnapshot
from src.readiness.readiness_store_v2 import (
    ReadinessViewStatus,
    read_snapshot,
    read_snapshot_sqlite,
)

_TEST_IDS = (99106, 99110, 99206)


@pytest.fixture
def sqlite_db(tmp_path: Path) -> Path:
    path = tmp_path / "readiness.db"
    con = sqlite3.connect(str(path))
    con.execute(
        """
        CREATE TABLE account_readiness_snapshots (
            id INTEGER PRIMARY KEY,
            account_id INTEGER UNIQUE,
            status TEXT NOT NULL,
            reason TEXT,
            failure_code TEXT,
            checked_at TEXT,
            expires_at TEXT
        )
        """
    )
    con.execute(
        "INSERT INTO account_readiness_snapshots VALUES (1, 106, 'ERROR', NULL, 'legacy_sqlite_session_format', '2026-05-16T00:00:00', NULL)"
    )
    con.execute(
        "INSERT INTO account_readiness_snapshots VALUES (2, 110, 'READY', NULL, NULL, '2026-05-16T00:00:00', NULL)"
    )
    con.commit()
    con.close()
    return path


@pytest.fixture
def orm_session(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'orm.db'}")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    for aid in _TEST_IDS:
        session.query(AccountReadinessSnapshot).filter(
            AccountReadinessSnapshot.account_id == aid
        ).delete()
        session.query(Account).filter(Account.id == aid).delete()
    session.add(
        Account(
            id=99106,
            phone_number="+10000000106",
            status=AccountStatus.ACTIVE,
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
    session.commit()
    try:
        yield session
    finally:
        session.close()


def test_sqlite_read_106_error(sqlite_db: Path) -> None:
    view = read_snapshot_sqlite(sqlite_db, 106)
    assert view.status == ReadinessViewStatus.ERROR
    assert view.failure_code == "legacy_sqlite_session_format"


def test_sqlite_read_110_ready(sqlite_db: Path) -> None:
    view = read_snapshot_sqlite(sqlite_db, 110)
    assert view.status == ReadinessViewStatus.READY


def test_sqlite_missing_table(tmp_path: Path) -> None:
    path = tmp_path / "empty.db"
    sqlite3.connect(str(path)).close()
    view = read_snapshot_sqlite(path, 1)
    assert view.status == ReadinessViewStatus.TABLE_MISSING


def test_sqlite_not_found(sqlite_db: Path) -> None:
    view = read_snapshot_sqlite(sqlite_db, 999)
    assert view.status == ReadinessViewStatus.NOT_FOUND


def test_orm_read(orm_session) -> None:
    view = read_snapshot(orm_session, 99106)
    assert view.status == ReadinessViewStatus.ERROR
    assert view.source == "orm"
