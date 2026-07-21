"""OTC profit evaluation, concessions, operator-ready, UI serializer."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from config.settings import settings
from src.ai_agent.otc_profit import (
    deal_size_tier,
    evaluate_buy_premium,
    evaluate_sell_premium,
    parse_premium_pct,
)
from src.ai_agent.serializers import serialize_profit_facts_for_ui
from src.ai_agent.strategy import (
    apply_operator_ready_fields,
    build_otc_policy_from_settings,
    merge_profit_strategy,
)


def policy(**kwargs):
    p = build_otc_policy_from_settings(settings)
    p.update(kwargs)
    return p


def test_parse_premium_pct():
    assert parse_premium_pct("+1%") == 1.0
    assert parse_premium_pct("0.3%") == 0.3
    assert parse_premium_pct(None) is None
    assert parse_premium_pct("Max 10000 usdt") is None
    assert parse_premium_pct("only cash +1%") == 1.0
    assert parse_premium_pct("365 -1%") == -1.0
    assert parse_premium_pct("premium 1%") == 1.0
    assert parse_premium_pct("commission 1.5%") == 1.5
    assert parse_premium_pct("fee -0.5%") == -0.5
    assert parse_premium_pct("rate +1%") == 1.0
    assert parse_premium_pct(10000.0) is None


def test_deal_size_tier():
    assert deal_size_tier(100, large=2000, small=500) == "small"
    assert deal_size_tier(1000, large=2000, small=500) == "medium"
    assert deal_size_tier(5000, large=2000, small=500) == "large"


def test_buy_0_3_excellent_accept():
    p = policy()
    r = evaluate_buy_premium(0.3, p)
    assert r["rate_evaluation"] == "excellent"
    assert r["profit_action"] == "accept"


def test_buy_0_6_acceptable():
    p = policy()
    r = evaluate_buy_premium(0.6, p)
    assert r["rate_evaluation"] == "acceptable"
    assert r["profit_action"] == "soft_negotiate"


def test_buy_1_0_negotiable_counter():
    p = policy()
    r = evaluate_buy_premium(1.0, p)
    assert r["rate_evaluation"] == "negotiable"
    assert r["profit_action"] == "counter"


def test_buy_2_0_bad():
    p = policy()
    r = evaluate_buy_premium(2.0, p)
    assert r["rate_evaluation"] == "bad"
    assert r["profit_action"] == "strong_counter"


def test_sell_mirrored():
    p = policy(target_sell_premium_pct=1.0, min_sell_premium_pct=0.4)
    assert evaluate_sell_premium(1.2, p)["rate_evaluation"] == "excellent"
    assert evaluate_sell_premium(0.9, p)["rate_evaluation"] == "acceptable"
    assert evaluate_sell_premium(0.35, p)["rate_evaluation"] == "negotiable"
    assert evaluate_sell_premium(-3.0, p)["rate_evaluation"] == "operator_review"


def test_merge_drops_corrupted_numeric_rate_premium():
    pol = policy()
    facts = merge_profit_strategy(
        {
            "side": "buy",
            "asset": "USDT",
            "amount_crypto": 1000,
            "rate_premium_pct": 10000.0,
        },
        policy=pol,
        latest_inbound_text="Max 10000 usdt",
    )
    assert facts.get("rate_premium_pct") != 10000.0
    assert facts.get("rate_premium_pct") is None or float(facts["rate_premium_pct"]) < 500


def test_concession_count_stops_counters():
    pol = policy()
    base = {"side": "buy", "asset": "USDT", "amount_crypto": 1000, "concession_count": 2}
    m = merge_profit_strategy(base, policy=pol, latest_inbound_text="+1%")
    assert m.get("next_counter_rate") is None
    assert m.get("final_best_requested") is True


def test_bad_rate_operator_ready_with_core_terms():
    pol = policy()
    facts = merge_profit_strategy(
        {
            "side": "buy",
            "asset": "USDT",
            "amount_crypto": 1000,
            "rate_premium_pct": 2.0,
        },
        policy=pol,
        latest_inbound_text="+2%",
    )
    assert facts["rate_evaluation"] == "bad"
    apply_operator_ready_fields(facts, pol)
    assert facts.get("negotiation_stage") == "ready_for_operator"
    assert facts.get("suggested_next_action") == "operator_review"
    assert "Rate is above configured max" in (facts.get("deal_summary") or "")


def test_ui_serializer_includes_profit_fields():
    raw = {
        "rate_evaluation": "negotiable",
        "target_rate": "+0.3%",
        "walkaway_rate": "+0.8%",
        "counter_offer_rate": "+0.65%",
        "deal_size_tier": "medium",
        "profit_action": "counter",
    }
    ui = serialize_profit_facts_for_ui(raw)
    assert ui["rate_evaluation"] == "negotiable"
    assert ui["target_rate"] == "+0.3%"
    assert "operator_decision_needed" not in ui


def test_manual_buy_1pct_draft_shape():
    task = SimpleNamespace(
        id=42,
        goal_text="buy 1000 USDT via TRC20 from this seller, get best rate and payment conditions",
        target_username_or_id="@seller",
        negotiation_stage="opening",
        tone="friendly",
        language="en",
    )
    from src.ai_agent.ai_client import _deterministic_draft

    hist = [{"role": "user", "content": "+1%"}]
    out = _deterministic_draft(task, hist, {})
    ef = out["extracted_facts"]
    assert ef.get("rate_evaluation") == "negotiable"
    assert ef.get("profit_action") == "counter"
    msg = out["draft_message"]
    assert "got it" in msg.lower()
    assert "trc20" in msg.lower()
    assert "+0.65%" in msg or "0.65%" in msg
    assert "closer to" in msg.lower()
    assert "which payment method works for you" in msg.lower()
    assert "at this rate" not in msg.lower()
    assert "confirm if you can provide" not in msg.lower()
    assert ef.get("negotiation_stage") == "negotiating_price"
    assert ef.get("strategy_phase") == "push"


def test_trc20_from_prior_assistant_history():
    """Payment rail can be recovered from sent outbound lines in thread history."""
    task = SimpleNamespace(
        id=101,
        goal_text="we need to bay 1000 usdt from this user",
        target_username_or_id="@x",
        negotiation_stage="opening",
        tone="neutral",
        language="en",
    )
    from src.ai_agent.ai_client import _deterministic_draft

    hist = [
        {"role": "assistant", "content": "buy 1000 USDT via TRC20 — what rate?"},
        {"role": "user", "content": "Hello +1%"},
    ]
    out = _deterministic_draft(task, hist, {})
    assert "TRC20" in out["draft_message"]


def test_counter_seeded_facts_and_sanitizer():
    from src.ai_agent.ai_client import _deterministic_draft, sanitize_profit_draft

    task = SimpleNamespace(
        id=99,
        goal_text="buy 1000 USDT via TRC20",
        target_username_or_id="@x",
        negotiation_stage="opening",
        tone="neutral",
        language="en",
    )
    hist = [{"role": "user", "content": "+1% confirmed"}]
    seeded = {
        "side": "buy",
        "asset": "USDT",
        "amount_crypto": 1000,
        "rate_premium_pct": 1.0,
        "profit_action": "counter",
        "counter_offer_rate": "+0.65%",
    }
    out = _deterministic_draft(task, hist, seeded)
    msg = out["draft_message"]
    assert "+0.65%" in msg
    assert "at this rate" not in msg.lower()

    toxic = (
        "Please confirm if you can provide 1000 USDT via TRC20 at this rate.\n"
        "Sounds good?"
    )
    eff = dict(seeded)
    eff["profit_action"] = "counter"
    cleaned = sanitize_profit_draft(toxic, eff)
    assert "at this rate" not in cleaned.lower()
    assert "confirm if you can provide" not in cleaned.lower()
    assert "+0.65%" in cleaned


def test_manual_buy_0_3_accept():
    task = SimpleNamespace(
        id=43,
        goal_text="buy 1000 USDT",
        target_username_or_id="@seller",
        negotiation_stage="opening",
        tone="friendly",
        language="en",
    )
    from src.ai_agent.ai_client import _deterministic_draft

    hist = [{"role": "user", "content": "+0.3%"}]
    out = _deterministic_draft(task, hist, {})
    ef = out["extracted_facts"]
    assert ef.get("rate_evaluation") == "excellent"
    assert ef.get("profit_action") == "accept"
    assert "payment" in out["draft_message"].lower()
