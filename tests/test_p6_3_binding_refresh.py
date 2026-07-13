"""P6.3 binding refresh lifecycle and verification tests (non-mutating vs production)."""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.clients.binding_verification import (
    CONFIGURED_NOT_VERIFIED,
    PROBE_MISSING,
    STALE_CHECK,
    VERIFIED_CAN_POST,
    classify_binding_verification,
)
from src.clients.membership_check import MEMBERSHIP_JOINED, check_target_membership_for_account


@pytest.mark.asyncio
async def test_membership_check_uses_connect_not_disconnected_add():
    mock_wrapper = MagicMock()
    mock_wrapper.client = MagicMock()
    with patch("src.clients.membership_check.client_manager.connect_account", new_callable=AsyncMock) as conn:
        with patch("src.clients.membership_check.client_manager.remove_account", new_callable=AsyncMock) as rm:
            conn.return_value = (mock_wrapper, None)
            with patch(
                "src.clients.membership_check.entity_probe_chain",
                return_value=[],
            ):
                row = await check_target_membership_for_account(107, 14)
    conn.assert_awaited_once()
    rm.assert_awaited_once_with(107)
    assert row["status"] != "error" or row.get("error") != "disconnected"


@pytest.mark.asyncio
async def test_refresh_binding_serializes_probe_slot():
    """Uses mocked membership probe; rolls back DB writes so production is untouched."""
    from src.core.database import get_db_context, init_db
    from src.dashboard.operator_control_service import refresh_binding_permission

    init_db()
    with get_db_context() as db:
        from src.core.scheduler_models import AccountTargetBinding, ScheduledJob

        binding = db.query(AccountTargetBinding).filter(AccountTargetBinding.id == 45).first()
        if binding is None:
            pytest.skip("binding 45 missing")
        before_jobs = db.query(ScheduledJob).count()
        with patch(
            "src.clients.membership_check.check_targets_membership_sequential",
            new_callable=AsyncMock,
            return_value=[{"target_id": 14, "status": MEMBERSHIP_JOINED, "can_post": True, "message": "ok"}],
        ):
            with patch("src.clients.readiness_worker._acquire_probe_slot", new_callable=AsyncMock, return_value=True):
                with patch("src.clients.readiness_worker._release_probe_slot", new_callable=AsyncMock):
                    with patch.object(db, "commit", lambda: None):
                        result = await refresh_binding_permission(db, 45)
                    after_jobs = db.query(ScheduledJob).count()
                    db.rollback()
    assert result.get("ok") is True
    assert before_jobs == after_jobs


def test_configured_can_post_without_probe_is_not_verified():
    """Pure unit: missing probe → not production-verified (no DB mutation)."""
    from src.clients.binding_verification import classify_binding_verification

    db = MagicMock()
    account = MagicMock()
    target = MagicMock()
    binding = MagicMock()
    binding.can_post = True
    binding.account_id = 107
    binding.target_id = 1

    snap = MagicMock()
    snap.status = "READY"

    def _query(model):
        name = getattr(model, "__name__", "") or getattr(model, "__tablename__", "")
        q = MagicMock()
        if name in ("Account", "accounts") or getattr(model, "__tablename__", "") == "accounts":
            q.filter.return_value.first.return_value = account
        elif name in ("ChatTarget",) or getattr(model, "__tablename__", "") == "chat_targets":
            q.filter.return_value.first.return_value = target
        elif name in ("AccountTargetBinding",) or getattr(model, "__tablename__", "") == "account_target_bindings":
            q.filter.return_value.first.return_value = binding
        elif name in ("AccountTargetMembershipProbe",) or getattr(model, "__tablename__", "") == "account_target_membership_probes":
            q.filter.return_value.order_by.return_value.first.return_value = None
        else:
            q.filter.return_value.first.return_value = None
        return q

    db.query.side_effect = _query
    with patch(
        "src.clients.binding_verification.compute_account_operational_state",
        return_value={"tier": "normal"},
    ), patch(
        "src.clients.binding_verification.readiness_store.fetch_snapshot",
        return_value=snap,
    ), patch(
        "src.clients.binding_verification.readiness_store.snapshot_ready_and_valid",
        return_value=True,
    ), patch(
        "src.clients.binding_verification.readiness_store.STAT_READY",
        "READY",
    ), patch(
        "src.clients.binding_verification.readiness_store.STAT_NOT_AUTH",
        "NOT_AUTH",
    ):
        ver = classify_binding_verification(db, 107, 1)
    assert ver["production_verified"] is False
    assert ver["status"] in (CONFIGURED_NOT_VERIFIED, PROBE_MISSING)


def test_stale_probe_blocks_production_verified():
    from src.core.database import get_db_context, init_db
    from src.core.scheduler_models import AccountTargetMembershipProbe

    init_db()
    with get_db_context() as db:
        probe = (
            db.query(AccountTargetMembershipProbe)
            .filter(AccountTargetMembershipProbe.account_id == 106, AccountTargetMembershipProbe.target_id == 1)
            .first()
        )
        if probe is None:
            pytest.skip("probe missing")
        ver = classify_binding_verification(db, 106, 1)
    assert ver["status"] == STALE_CHECK
    assert ver["production_verified"] is False


def test_eligibility_denies_unverified_or_blocks_without_fresh_verified():
    from src.core.database import get_db_context, init_db
    from src.scheduler.generation_eligibility import evaluate_generation_eligibility

    init_db()
    with get_db_context() as db:
        decision = evaluate_generation_eligibility(
            db,
            job_type="PROMO",
            account_id=107,
            target_id=1,
            generation_scope="p5c_certification",
            job_marker="P5C_SCOPED_SINGLE_SEND",
        )
    # Allowed only if production-verified + fresh; otherwise must deny.
    if decision.allowed:
        ver_ok = True
        from src.clients.binding_verification import classify_binding_verification

        with get_db_context() as db:
            ver = classify_binding_verification(db, 107, 1)
        assert ver.get("production_verified") is True
        assert ver.get("status") == VERIFIED_CAN_POST
    else:
        assert decision.reason_code in (
            "binding_not_verified",
            "binding_check_stale",
            "unresolved_target",
            "binding_probe_inconclusive",
            "account_not_ready",
            "readiness_stale",
            "not_joined",
            "no_permission_to_post",
        )
