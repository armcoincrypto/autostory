"""P6.2 — Background readiness worker activation tests."""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from src.clients import readiness_store
from src.clients.readiness_worker import (
    _acquire_probe_slot,
    _deep_check_one,
    _release_probe_slot,
    probe_slot_in_flight,
    readiness_worker_loop,
)
from src.clients.readiness_worker_policy import (
    ReadinessWorkerConfig,
    account_probe_due,
    select_probe_candidates,
)
from src.core.database import get_db_context, init_db
from src.core.models import Account
from src.core.scheduler_models import AccountReadinessSnapshot, MessageDelivery, ScheduledJob
from src.telegram_gateway.models import TelegramGatewayJob
from src.dashboard.operator_control_service import build_system_safety_snapshot, queue_counts_snapshot


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _init_db():
    init_db()


def test_config_validation_rejects_bad_timeout(monkeypatch):
    monkeypatch.setenv("READINESS_WORKER_PER_ACCOUNT_TIMEOUT_SEC", "3")
    monkeypatch.setenv("READINESS_WORKER_SESSION_LOCK_TIMEOUT_SEC", "12")
    cfg = ReadinessWorkerConfig.from_environ()
    errors = cfg.validate()
    assert errors


def test_not_authorized_account_respects_auth_backoff(monkeypatch):
    monkeypatch.setenv("READINESS_WORKER_AUTH_FAILURE_BACKOFF_SEC", "3600")
    with get_db_context() as db:
        acc = db.query(Account).filter(Account.id == 139).first()
        if acc is None:
            pytest.skip("account 139 missing")
        now = _utc_now()
        snap = readiness_store.fetch_snapshot(db, 139)
        if snap is None or snap.status != readiness_store.STAT_NOT_AUTH:
            readiness_store.mark_account_not_authorized(db, 139, "test_not_auth")
            snap = readiness_store.fetch_snapshot(db, 139)
        due, reason = account_probe_due(db, acc, now=now)
        assert due is False
        assert reason == "auth_failure_backoff"


def test_fresh_ready_skipped(monkeypatch):
    with get_db_context() as db:
        acc = db.query(Account).filter(Account.id == 107).first()
        if acc is None:
            pytest.skip("account 107 missing")
        readiness_store.mark_account_ready_after_success(db, 107, "test_ready")
        due, reason = account_probe_due(db, acc, now=_utc_now())
        assert due is False
        assert reason == "fresh_ready"


def test_batch_size_limits_candidates(monkeypatch):
    monkeypatch.setenv("READINESS_WORKER_BATCH_SIZE", "2")
    monkeypatch.setenv("READINESS_WORKER_ALLOW_IDS", "107,139")
    with get_db_context() as db:
        accounts = db.query(Account).order_by(Account.id).all()
        ids, _ = select_probe_candidates(db, accounts)
        assert len(ids) <= 2


@pytest.mark.asyncio
async def test_probe_slot_prevents_overlap():
    assert await _acquire_probe_slot(999001) is True
    assert probe_slot_in_flight(999001) is True
    assert await _acquire_probe_slot(999001, wait=False) is False
    await _release_probe_slot(999001)
    assert probe_slot_in_flight(999001) is False


@pytest.mark.asyncio
async def test_dry_run_does_not_mutate_readiness(monkeypatch):
    monkeypatch.setenv("READINESS_WORKER_DRY_RUN", "true")
    with get_db_context() as db:
        before = db.query(AccountReadinessSnapshot).filter(AccountReadinessSnapshot.account_id == 107).count()
        before_jobs = queue_counts_snapshot(db)
    with patch("src.clients.readiness_worker.client_manager.connect_account", new_callable=AsyncMock) as conn:
        conn.return_value = (object(), None)
        result = await _deep_check_one(107, dry_run=True)
    assert result == "dry_run"
    with get_db_context() as db:
        after = db.query(AccountReadinessSnapshot).filter(AccountReadinessSnapshot.account_id == 107).count()
        after_jobs = queue_counts_snapshot(db)
    assert after == before
    assert after_jobs == before_jobs
    conn.assert_not_called()


@pytest.mark.asyncio
async def test_deep_check_mocked_does_not_touch_forbidden_tables(monkeypatch):
    from tests.helpers.fleet_seed import seed_minimal_fleet

    init_db()
    with get_db_context() as db:
        seed_minimal_fleet(db, account_ids=(107,), seed_mentions=False, seed_target=False)
        before_jobs = db.query(ScheduledJob).count()
        before_gw = db.query(TelegramGatewayJob).count()
        before_del = db.query(MessageDelivery).count()
        before_max_job = db.query(ScheduledJob.id).order_by(ScheduledJob.id.desc()).limit(1).scalar() or 0
    with patch("src.clients.readiness_worker.client_manager.connect_account", new_callable=AsyncMock) as conn:
        conn.return_value = (object(), None)
        with patch("src.clients.readiness_worker.is_account_active", return_value=False):
            result = await _deep_check_one(107)
    assert result == "ready"
    with get_db_context() as db:
        assert db.query(ScheduledJob).count() == before_jobs
        assert db.query(TelegramGatewayJob).count() == before_gw
        assert db.query(MessageDelivery).count() == before_del
        assert (db.query(ScheduledJob.id).order_by(ScheduledJob.id.desc()).limit(1).scalar() or 0) == before_max_job


@pytest.mark.asyncio
async def test_worker_run_once_dry_cycle(monkeypatch):
    monkeypatch.setenv("READINESS_WORKER_RUN_ONCE", "true")
    monkeypatch.setenv("READINESS_WORKER_DRY_RUN", "true")
    monkeypatch.setenv("READINESS_WORKER_ALLOW_IDS", "107")
    monkeypatch.setenv("READINESS_WORKER_CYCLE_SEC", "1")
    with get_db_context() as db:
        before = queue_counts_snapshot(db)
    await readiness_worker_loop()
    with get_db_context() as db:
        after = queue_counts_snapshot(db)
    assert before == after


def test_system_safety_includes_readiness_worker_block():
    with get_db_context() as db:
        snap = build_system_safety_snapshot(db)
    assert "readiness_worker" in snap
    assert "autostory-readiness-worker" in snap["services"]
    assert "fresh_readiness_count" in snap


def test_stale_ready_becomes_due(monkeypatch):
    monkeypatch.setenv("READINESS_TRUST_WINDOW_SEC", "60")
    with get_db_context() as db:
        acc = db.query(Account).filter(Account.id == 107).first()
        if acc is None:
            pytest.skip("account 107 missing")
        readiness_store.mark_account_ready_after_success(db, 107, "stale_test")
        row = db.query(AccountReadinessSnapshot).filter(AccountReadinessSnapshot.account_id == 107).first()
        assert row is not None
        row.checked_at = _utc_now() - timedelta(seconds=120)
        row.expires_at = _utc_now() - timedelta(seconds=30)
        db.commit()
        due, reason = account_probe_due(db, acc, now=_utc_now())
    assert due is True
    assert reason in ("stale_ready", "expired_snapshot")
