"""P5D gateway restart durability tests."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.p5d_failpoints import P5DFailpointStop, p5d_certification_mode, p5d_hit_failpoint
from src.core.p5d_gateway_reconciliation import (
    NOT_SENT_CONFIRMED,
    SENT_CONFIRMED,
    classify_certification_payload,
    pre_send_certification_guard,
)


def test_failpoint_inactive_by_default(monkeypatch):
    monkeypatch.delenv("P5D_CERTIFICATION_MODE", raising=False)
    monkeypatch.delenv("P5D_FAILPOINT", raising=False)
    assert not p5d_certification_mode()
    p5d_hit_failpoint("p5d_after_gateway_claim")


def test_failpoint_raises_when_armed(monkeypatch):
    monkeypatch.setenv("P5D_CERTIFICATION_MODE", "true")
    monkeypatch.setenv("P5D_FAILPOINT", "p5d_after_gateway_claim")
    with pytest.raises(P5DFailpointStop):
        p5d_hit_failpoint("p5d_after_gateway_claim")


def test_classify_certification_payload():
    assert classify_certification_payload({"p5d_certification": True})
    assert classify_certification_payload({"p5c_certification": True})
    assert not classify_certification_payload({})


@pytest.mark.asyncio
async def test_pre_send_guard_not_certification():
    guard = await pre_send_certification_guard(
        gateway_job_id=1, account_id=107, target="8000295303", payload={}
    )
    assert guard.outcome == NOT_SENT_CONFIRMED


@pytest.mark.asyncio
async def test_pre_send_guard_gateway_done(monkeypatch):
    monkeypatch.setattr(
        "src.core.p5d_gateway_reconciliation.get_job",
        lambda _id: type("J", (), {"status": "done", "result_json": {"telegram_message_id": 99}})(),
    )

    async def _no_lookup(*_a, **_k):
        return None

    monkeypatch.setattr(
        "src.core.p5d_gateway_reconciliation._lookup_message_by_body",
        _no_lookup,
    )
    guard = await pre_send_certification_guard(
        gateway_job_id=5,
        account_id=107,
        target="8000295303",
        payload={"p5d_certification": True, "text": "hello"},
        db=None,
    )
    assert guard.outcome == SENT_CONFIRMED
    assert guard.tg_message_id == 99


@pytest.mark.asyncio
async def test_pre_send_guard_telegram_lookup(monkeypatch):
    monkeypatch.setattr(
        "src.core.p5d_gateway_reconciliation.get_job",
        lambda _id: type("J", (), {"status": "retry", "result_json": {}, "attempts": 1})(),
    )

    async def _lookup(_aid, _target, body):
        return 42 if "scenario C" in body else None

    monkeypatch.setattr(
        "src.core.p5d_gateway_reconciliation._lookup_message_by_body",
        _lookup,
    )
    guard = await pre_send_certification_guard(
        gateway_job_id=6,
        account_id=107,
        target="8000295303",
        payload={"p5d_certification": True, "p5d_scenario": "C", "text": "scenario C test"},
        db=None,
    )
    assert guard.outcome == SENT_CONFIRMED
    assert guard.tg_message_id == 42
    assert guard.reason_code == "telegram_lookup_confirmed_sent"
