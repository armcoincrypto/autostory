"""P9.7 v1 vs v2 comparison logic (unit + isolated DB)."""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.database import Base
from src.core.models import Account, AccountStatus
from src.core.scheduler_models import AccountReadinessSnapshot
from src.readiness.readiness_store_v2 import write_snapshot
from src.readiness.readiness_v2_observer import (
    compare_readiness_v1_v2,
    explain_v1_v2_mismatch,
)

# Reserved tier id used in production pilot
_RESERVED_ID = 113
_CONTROLLER_ID = 207
_FLEET_LEGACY_ID = 99106


@pytest.mark.parametrize(
    "v1,v2,fc,tier,expected",
    [
        ("ERROR", "LEGACY_SCHEMA", "legacy_sqlite_session_format", "fleet", "acceptable_legacy_semantics"),
        ("READY", "RESERVED", None, "reserved", "intentional_tier_reserved"),
        ("READY", "CONTROLLER", None, "controller", "intentional_tier_controller"),
        ("READY", "READY", None, "fleet", "match"),
        ("READY", "NOT_FOUND", None, "fleet", "no_v2_snapshot"),
    ],
)
def test_explain_mismatch(v1, v2, fc, tier, expected) -> None:
    assert (
        explain_v1_v2_mismatch(
            v1_status=v1,
            v2_status=v2,
            v1_failure_code=fc,
            tier=tier,
        )
        == expected
    )


@pytest.fixture
def compare_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'cmp.db'}")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    for aid in (_FLEET_LEGACY_ID, _RESERVED_ID, _CONTROLLER_ID):
        session.query(AccountReadinessSnapshot).filter(
            AccountReadinessSnapshot.account_id == aid
        ).delete()
        session.query(Account).filter(Account.id == aid).delete()
    session.add(
        Account(
            id=_FLEET_LEGACY_ID,
            phone_number="+10000000106",
            status=AccountStatus.ACTIVE,
            purpose="both",
        )
    )
    session.add(
        Account(
            id=_RESERVED_ID,
            phone_number="+10000000113",
            status=AccountStatus.ACTIVE,
            purpose="both",
        )
    )
    session.add(
        Account(
            id=_CONTROLLER_ID,
            phone_number="+10000000207",
            status=AccountStatus.ACTIVE,
            purpose="both",
        )
    )
    session.add(
        AccountReadinessSnapshot(
            account_id=_FLEET_LEGACY_ID,
            status="ERROR",
            failure_code="legacy_sqlite_session_format",
            checked_at=datetime.utcnow(),
        )
    )
    session.add(
        AccountReadinessSnapshot(
            account_id=_RESERVED_ID,
            status="READY",
            checked_at=datetime.utcnow(),
        )
    )
    session.add(
        AccountReadinessSnapshot(
            account_id=_CONTROLLER_ID,
            status="READY",
            checked_at=datetime.utcnow(),
        )
    )
    session.commit()
    write_snapshot(
        session,
        _FLEET_LEGACY_ID,
        "LEGACY_SCHEMA",
        failure_code="legacy_sqlite_session_format",
        reason="v8_incompatible",
        dry_run=False,
    )
    write_snapshot(
        session,
        _RESERVED_ID,
        "RESERVED",
        reason="reserved_account_immutable",
        dry_run=False,
    )
    write_snapshot(
        session,
        _CONTROLLER_ID,
        "CONTROLLER",
        reason="controller_account_operator_critical",
        dry_run=False,
    )
    try:
        yield session
    finally:
        session.close()


def test_compare_rows(compare_db) -> None:
    rows = compare_readiness_v1_v2(
        compare_db, [_FLEET_LEGACY_ID, _RESERVED_ID, _CONTROLLER_ID]
    )
    by_id = {r["account_id"]: r for r in rows}
    assert by_id[_FLEET_LEGACY_ID]["mismatch_reason"] == "acceptable_legacy_semantics"
    assert by_id[_RESERVED_ID]["mismatch_reason"] == "intentional_tier_reserved"
    assert by_id[_CONTROLLER_ID]["mismatch_reason"] == "intentional_tier_controller"
