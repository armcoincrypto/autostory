"""P9.7 readiness v2 API — feature flag, NOT_FOUND, no writes."""
from __future__ import annotations

import importlib
from datetime import datetime

import pytest
from flask import Flask
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.database import Base
from src.core.models import Account, AccountStatus
from src.core.scheduler_models import AccountReadinessSnapshot
from src.readiness.readiness_models_v2 import AccountReadinessSnapshotV2
from src.readiness.readiness_store_v2 import write_snapshot

_TEST_IDS = (99071, 99072)


@pytest.fixture
def db_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'api.db'}")
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
    session.add(
        Account(
            id=99071,
            phone_number="+10000099071",
            status=AccountStatus.ACTIVE,
            purpose="both",
        )
    )
    session.add(
        AccountReadinessSnapshot(
            account_id=99071,
            status="READY",
            checked_at=datetime.utcnow(),
        )
    )
    session.commit()
    try:
        yield session
    finally:
        session.close()


def _minimal_app(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> Flask:
    settings_mod = importlib.import_module("config.settings")
    monkeypatch.setattr(settings_mod.settings, "readiness_v2_api_enabled", enabled)
    routes_mod = importlib.import_module("src.dashboard.readiness_v2_routes")
    monkeypatch.setattr(routes_mod, "settings", settings_mod.settings)
    monkeypatch.setattr(routes_mod, "dashboard_api_authorized", lambda: True)

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(routes_mod.readiness_v2_api)
    return app


def test_readiness_v2_api_disabled_404(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _minimal_app(monkeypatch, False)
    client = app.test_client()
    r = client.get("/api/readiness/v2/snapshots?ids=99071")
    assert r.status_code == 404


def test_readiness_v2_api_not_found(
    monkeypatch: pytest.MonkeyPatch, db_session
) -> None:
    monkeypatch.setattr(
        "src.dashboard.readiness_v2_routes.get_db_context",
        lambda: _ctx(db_session),
    )
    app = _minimal_app(monkeypatch, True)
    client = app.test_client()
    r = client.get("/api/readiness/v2/snapshots?ids=99071")
    assert r.status_code == 200
    data = r.get_json()
    assert data["authoritative"] is False
    snap = data["snapshots"][0]
    assert snap["account_id"] == 99071
    assert snap["v2_status"] == "NOT_FOUND"
    assert "no_v2_snapshot" in snap["warnings"]


def test_readiness_v2_api_returns_row(
    monkeypatch: pytest.MonkeyPatch, db_session
) -> None:
    write_snapshot(
        db_session,
        99071,
        "RESERVED",
        reason="test_reserved",
        dry_run=False,
    )
    monkeypatch.setattr(
        "src.dashboard.readiness_v2_routes.get_db_context",
        lambda: _ctx(db_session),
    )
    app = _minimal_app(monkeypatch, True)
    client = app.test_client()
    r = client.get("/api/readiness/v2/snapshots?ids=99071")
    assert r.status_code == 200
    snap = r.get_json()["snapshots"][0]
    assert snap["v2_status"] == "RESERVED"
    assert snap["v2_reason"] == "test_reserved"
    assert snap["checked_at"] is not None


def test_readiness_v2_api_no_writes_on_get(
    monkeypatch: pytest.MonkeyPatch, db_session
) -> None:
    before = (
        db_session.query(AccountReadinessSnapshotV2)
        .filter(AccountReadinessSnapshotV2.account_id == 99072)
        .count()
    )
    monkeypatch.setattr(
        "src.dashboard.readiness_v2_routes.get_db_context",
        lambda: _ctx(db_session),
    )
    app = _minimal_app(monkeypatch, True)
    app.test_client().get("/api/readiness/v2/snapshots?ids=99072")
    after = (
        db_session.query(AccountReadinessSnapshotV2)
        .filter(AccountReadinessSnapshotV2.account_id == 99072)
        .count()
    )
    assert before == after == 0


class _ctx:
    def __init__(self, session):
        self._session = session

    def __enter__(self):
        return self._session

    def __exit__(self, *args):
        return False
