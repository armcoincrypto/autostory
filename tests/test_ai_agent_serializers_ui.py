"""Serializer-derived UI fields for AI Agent dashboard."""
from __future__ import annotations

from types import SimpleNamespace

from src.ai_agent.serializers import (
    build_ui_deal_summary,
    serialize_profit_facts_for_ui,
    serialize_task_full,
)


def _task(**kwargs: object) -> SimpleNamespace:
    base = dict(
        id=33,
        account_id=110,
        target_username_or_id="@seller",
        goal_text="buy usdt cash",
        language="auto",
        tone="professional",
        max_messages=10,
        status="waiting_admin_approval",
        negotiation_stage="opening",
        auto_mode="autonomous",
        auto_delay_sec=20,
        auto_last_run_at=None,
        final_summary=None,
        last_activity_at=None,
        created_at=None,
        updated_at=None,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_ready_for_operator_ui_status_and_primary():
    t = _task(status="waiting_admin_approval")
    facts = {
        "negotiation_stage": "ready_for_operator",
        "side": "buy",
        "asset": "USDT",
        "rate_premium_pct": -1.0,
        "payment_method": "cash",
        "fiat_currency": "AMD",
        "location": "Yerevan, Arshakunyac 40",
        "meeting_time": "10:00",
        "transaction_limit": "Max 10000 USDT",
        "kyc_required": True,
    }
    pf = serialize_profit_facts_for_ui(facts)
    out = serialize_task_full(t, facts=facts, profit_facts=pf)
    assert out["ui_status_label"] == "Ready"
    assert out["ui_primary_action"] == "review_deal"
    assert out["ui_is_operator_ready"] is True
    assert out["ui_filter_status"] == "ready_for_operator"
    assert out["ui_next_action"] == "Review deal"
    assert out["ui_recommended_action"] == "Proceed to operator review."
    ds = out["ui_deal_summary"]
    assert ds["instrument"] == "Buy USDT"
    assert ds["rate"] == "-1%"
    assert "Cash" in ds["payment"]
    assert ds["currency"] == "AMD"
    assert "Yerevan" in ds["location"]
    assert ds["time"] == "10:00"
    assert "10000" in ds["limit"]
    assert "Passport" in ds["kyc"]
    assert out["ui_escalation_reason"] == "Meeting details confirmed."
    assert 70 <= int(out["ui_deal_completion_pct"]) <= 100


def test_draft_ready_not_operator_primary_is_send():
    t = _task(status="waiting_admin_approval", negotiation_stage="negotiating_price")
    facts = {"negotiation_stage": "negotiating_price", "side": "buy"}
    pf = serialize_profit_facts_for_ui(facts)
    out = serialize_task_full(t, facts=facts, profit_facts=pf)
    assert out["ui_primary_action"] == "send"
    assert out["ui_is_operator_ready"] is False


def test_list_summary_prefers_deal_summary():
    t = _task()
    facts = {"deal_summary": "OTC snapshot: rate=1% pay=cash loc=Yerevan", "negotiation_stage": "opening"}
    out = serialize_task_full(t, facts=facts, profit_facts={})
    assert "OTC snapshot" in out["ui_list_summary"]


def test_completed_primary_none():
    t = _task(status="completed", negotiation_stage="completed")
    out = serialize_task_full(t, facts={}, profit_facts={})
    assert out["ui_primary_action"] == "none"
    assert out["ui_status_label"] == "Completed"


def test_build_ui_deal_summary_empty():
    d = build_ui_deal_summary({})
    assert d == {}


def test_ui_deal_summary_rows_have_slugs():
    facts = {"side": "sell", "asset": "btc", "fiat_currency": "usd"}
    out = serialize_task_full(_task(), facts=facts, profit_facts={})
    rows = out["ui_deal_summary_rows"]
    assert rows and all("slug" in r and "label" in r and "value" in r for r in rows)
    assert {r["slug"] for r in rows} >= {"instrument", "currency"}
