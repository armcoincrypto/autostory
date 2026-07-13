"""P6.3 binding refresh lifecycle and verification tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.clients.binding_verification import (
    CONFIGURED_NOT_VERIFIED,
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
    from src.core.database import get_db_context, init_db
    from src.dashboard.operator_control_service import refresh_binding_permission

    init_db()
    with get_db_context() as db:
        from src.core.scheduler_models import AccountTargetBinding

        binding = db.query(AccountTargetBinding).filter(AccountTargetBinding.id == 45).first()
        if binding is None:
            pytest.skip("binding 45 missing")
        before_jobs = db.query(AccountTargetBinding).count()
    with patch(
        "src.clients.membership_check.check_targets_membership_sequential",
        new_callable=AsyncMock,
        return_value=[{"target_id": 14, "status": MEMBERSHIP_JOINED, "can_post": True, "message": "ok"}],
    ):
        with patch("src.clients.readiness_worker._acquire_probe_slot", new_callable=AsyncMock, return_value=True):
            with patch("src.clients.readiness_worker._release_probe_slot", new_callable=AsyncMock):
                with get_db_context() as db:
                    from src.core.scheduler_models import ScheduledJob

                    before_jobs = db.query(ScheduledJob).count()
                    result = await refresh_binding_permission(db, 45)
                    after_jobs = db.query(ScheduledJob).count()
    assert result.get("ok") is True
    assert before_jobs == after_jobs


def test_configured_can_post_without_probe_is_not_verified():
    from src.core.database import get_db_context, init_db
    from src.core.scheduler_models import AccountTargetBinding, AccountTargetMembershipProbe

    init_db()
    with get_db_context() as db:
        binding = db.query(AccountTargetBinding).filter(AccountTargetBinding.account_id == 107).first()
        if binding is None:
            pytest.skip("no binding for 107")
        db.query(AccountTargetMembershipProbe).filter(
            AccountTargetMembershipProbe.account_id == binding.account_id,
            AccountTargetMembershipProbe.target_id == binding.target_id,
        ).delete()
        db.commit()
        ver = classify_binding_verification(db, int(binding.account_id), int(binding.target_id))
    assert ver["production_verified"] is False
    assert ver["status"] in (CONFIGURED_NOT_VERIFIED, STALE_CHECK)


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


def test_eligibility_denies_unverified_binding():
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
    assert decision.allowed is False
    assert decision.reason_code in (
        "binding_not_verified",
        "binding_check_stale",
        "unresolved_target",
        "binding_probe_inconclusive",
    )
