"""P9.4 manager_v2 — metadata only, no Telethon."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from src.clients.manager_v2 import get_account_metadata, summarize_fleet_slice
from src.core.database import Base, engine
from src.core.models import Account, AccountStatus
from src.core.scheduler_models import AccountReadinessSnapshot
from src.clients.session_resolve import ERR_LEGACY_SQLITE_SESSION_FORMAT

_TEST_IDS = (99106, 99110, 99206)


def _write_v8_session(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE version (version INTEGER PRIMARY KEY)")
    con.execute("INSERT INTO version VALUES (8)")
    con.execute(
        """CREATE TABLE sessions (
        dc_id INTEGER PRIMARY KEY,
        server_address TEXT, port INTEGER,
        auth_key BLOB, takeout_id INTEGER, tmp_auth_key BLOB)"""
    )
    con.execute("INSERT INTO sessions VALUES (1,'x',443,x'00',NULL,NULL)")
    con.commit()
    con.close()


@pytest.fixture
def db_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    for aid in _TEST_IDS:
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
            health_status="error",
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
        for aid in _TEST_IDS:
            session.query(AccountReadinessSnapshot).filter(
                AccountReadinessSnapshot.account_id == aid
            ).delete()
            session.query(Account).filter(Account.id == aid).delete()
        session.commit()
        session.close()


def test_metadata_106_fleet_v8(db_session, tmp_path: Path) -> None:
    meta = get_account_metadata(
        db_session,
        99106,
        include_lock=True,
        locks_dir=str(tmp_path / "locks"),
    )
    assert meta["found"] is True
    assert meta["tier"] == "fleet"
    assert meta["schema_version"] == 8
    assert meta["resolver_code"] == ERR_LEGACY_SQLITE_SESSION_FORMAT
    assert meta["scheduler_eligible"] is False
    assert meta["readiness_snapshot_status"] == "ERROR"


def test_metadata_110_reserved(db_session, tmp_path: Path) -> None:
    meta = get_account_metadata(db_session, 99110, locks_dir=str(tmp_path / "locks"))
    assert meta["tier"] == "reserved"
    assert meta["is_reserved"] is True
    assert meta["scheduler_eligible"] is False


def test_metadata_206_controller(db_session, tmp_path: Path) -> None:
    meta = get_account_metadata(db_session, 99206, locks_dir=str(tmp_path / "locks"))
    assert meta["tier"] == "controller"
    assert meta["is_controller"] is True
    assert meta["scheduler_eligible"] is False


def test_summarize_fleet_slice(db_session) -> None:
    summary = summarize_fleet_slice(db_session, [99106, 99110, 99206])
    assert summary["count"] == 3
    assert summary["by_tier"].get("fleet") == 1
    assert summary["by_tier"].get("reserved") == 1
    assert summary["by_tier"].get("controller") == 1
