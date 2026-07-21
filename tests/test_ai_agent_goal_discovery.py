"""Goal mode / collection mode and discovery-first drafts (OTC ops)."""
from __future__ import annotations

from types import SimpleNamespace

from src.ai_agent.ai_client import (
    _deterministic_body_from_profit,
    _deterministic_draft,
    _infer_facts_from_goal,
    _merge_base_facts,
)
from src.ai_agent.goal_modes import (
    infer_goal_collection_modes,
    infer_payment_method_from_goal,
    price_negotiation_counters_allowed,
)
from src.ai_agent.strategy import build_otc_policy_from_settings, merge_profit_strategy


def _question_mark_count(s: str) -> int:
    return (s or "").count("?")


def _operational_keyword_hits(s: str) -> int:
    low = (s or "").lower()
    keys = ("limits", "verification", "timing", "kyc", "sequence", "safety")
    return sum(1 for k in keys if k in low)


def test_goal_full_information_cash_classification():
    g = "we need to bay usdt get full information i have cash"
    m = infer_goal_collection_modes(g)
    assert m["goal_mode"] == "process_discovery"
    assert m["collection_mode"] == "cash_exchange_discovery"
    assert infer_payment_method_from_goal(g) == "cash"
    inf = _infer_facts_from_goal(g)
    assert inf.get("side") == "buy"
    assert inf.get("asset") == "USDT"
    assert inf.get("amount_crypto") is None
    assert inf.get("payment_method") == "cash"


def test_seller_cash_plus_one_draft_no_counter_no_payment_method_question():
    goal = "we need to bay usdt get full information i have cash"
    task = SimpleNamespace(goal_text=goal, id=33)
    policy = build_otc_policy_from_settings()
    seller = "I can sell only with cash +1%"
    eff = _merge_base_facts(None, task)
    eff = merge_profit_strategy(eff, policy=policy, latest_inbound_text=seller)
    assert eff.get("goal_mode") == "process_discovery"
    assert eff.get("payment_method") == "cash"
    assert eff.get("profit_action") != "counter"
    body = _deterministic_body_from_profit(
        eff,
        policy,
        user_txt=seller,
        goal=goal,
        history=[{"role": "user", "content": seller}],
    )
    low = body.lower()
    assert "closer to" not in low
    assert "which payment method" not in low
    assert "for  usdt" not in low.replace(" ", " ")  # no double-space amount hole
    assert "cash" in low and "+1%" in body
    assert _question_mark_count(body) <= 2
    assert _operational_keyword_hits(body) <= 0
    assert "got it, cash and +1%" in low
    assert "where can we meet, and what cash currency" not in low


def test_amount_missing_no_double_space_usdt_in_counter_leg():
    policy = build_otc_policy_from_settings()
    eff = {
        "side": "buy",
        "asset": "USDT",
        "amount_crypto": None,
        "rate_premium_pct": 1.0,
        "payment_method": None,
        "goal_mode": "deal_negotiation",
        "collection_mode": None,
        "goal_text": "buy usdt",
        "concession_count": 0,
        "negotiation_stage": "opening",
    }
    eff = merge_profit_strategy(eff, policy=policy, latest_inbound_text="+1%")
    # Force counter path for message shape (amount still unknown)
    eff["profit_action"] = "counter"
    eff["next_counter_rate"] = "+0.73%"
    eff["counter_offer_rate"] = "+0.73%"
    from src.ai_agent.ai_client import _build_buy_counter_message

    msg = _build_buy_counter_message(
        eff,
        policy,
        user_txt="+1%",
        goal="buy usdt",
        history=[],
    )
    assert "For  USDT" not in msg
    assert "For this USDT leg" in msg or "this USDT" in msg


def test_cash_discovery_first_after_rate_is_short_two_part_question():
    goal = "we need to bay usdt get full information i have cash"
    task = SimpleNamespace(goal_text=goal)
    policy = build_otc_policy_from_settings()
    seller = "only cash +1%"
    eff = _merge_base_facts(None, task)
    eff = merge_profit_strategy(eff, policy=policy, latest_inbound_text=seller)
    body = _deterministic_body_from_profit(
        eff,
        policy,
        user_txt=seller,
        goal=goal,
        history=[{"role": "user", "content": seller}],
    )
    low = body.lower()
    assert _question_mark_count(body) <= 2
    assert "got it, cash and +1%" in low
    assert "amd cash only" in low or "where works" in low
    assert _operational_keyword_hits(body) == 0


def test_process_discovery_merge_does_not_counter_one_percent():
    goal = "we need to bay usdt get full information i have cash"
    task = SimpleNamespace(goal_text=goal)
    policy = build_otc_policy_from_settings()
    eff = _merge_base_facts(None, task)
    eff = merge_profit_strategy(eff, policy=policy, latest_inbound_text="only cash +1%")
    assert eff.get("profit_action") in (None, "collect_process", "soft_negotiate", "accept")
    assert eff.get("profit_action") not in ("counter", "strong_counter")
    goal = "buy usdt via bank card need full information china bank usd transfer"
    task = SimpleNamespace(goal_text=goal)
    policy = build_otc_policy_from_settings()
    eff = _merge_base_facts(None, task)
    eff = merge_profit_strategy(eff, policy=policy, latest_inbound_text="We can do +0.8%")
    body = _deterministic_body_from_profit(
        eff,
        policy,
        user_txt="We can do +0.8%",
        goal=goal,
        history=[{"role": "user", "content": "We can do +0.8%"}],
    )
    low = body.lower()
    assert "country" in low or "bank" in low
    assert "selling usdt to us on this leg" not in low
    assert "closer to" not in low
    assert _question_mark_count(body) <= 2
    assert (
        "which country is the card from" in low
        or "account in usd" in low
        or "which country/bank is the card from" in low
    )


def test_deal_negotiation_with_amount_still_counters():
    goal = "buy 1000 usdt trc20"
    task = SimpleNamespace(goal_text=goal)
    policy = build_otc_policy_from_settings()
    eff = _merge_base_facts(None, task)
    assert eff.get("goal_mode") == "deal_negotiation"
    assert eff.get("amount_crypto") == 1000.0
    eff = merge_profit_strategy(eff, policy=policy, latest_inbound_text="I sell at +1%")
    assert price_negotiation_counters_allowed(eff) is True
    assert eff.get("profit_action") in ("counter", "strong_counter", "soft_negotiate", "accept")


def test_cash_plus_rate_never_bundles_five_operational_topics():
    """Regression: one message must not ask location + currency + limits + verification + timing together."""
    goal = "we need to bay usdt get full information i have cash"
    task = SimpleNamespace(goal_text=goal)
    policy = build_otc_policy_from_settings()
    seller = "I can sell only with cash +1%"
    eff = _merge_base_facts(None, task)
    eff = merge_profit_strategy(eff, policy=policy, latest_inbound_text=seller)
    body = _deterministic_body_from_profit(
        eff,
        policy,
        user_txt=seller,
        goal=goal,
        history=[{"role": "user", "content": seller}],
    )
    low = body.lower()
    bundle = ("location", "limits", "verification", "timing", "currency")
    hits = sum(1 for w in bundle if w in low)
    assert hits < 5


def test_process_discovery_general_two_questions_max():
    goal = "buy usdt get full information how it works"
    task = SimpleNamespace(goal_text=goal)
    policy = build_otc_policy_from_settings()
    eff = _merge_base_facts(None, task)
    eff = merge_profit_strategy(eff, policy=policy, latest_inbound_text="We work at +0.9%")
    body = _deterministic_body_from_profit(
        eff,
        policy,
        user_txt="We work at +0.9%",
        goal=goal,
        history=[{"role": "user", "content": "We work at +0.9%"}],
    )
    assert _question_mark_count(body) <= 2


def test_task_33_style_line_from_deterministic_draft():
    goal = "we need to bay usdt get full information i have cash"
    task = SimpleNamespace(
        id=33,
        goal_text=goal,
        target_username_or_id="@seller",
        tone="professional",
        language="en",
    )
    hist = [
        {
            "role": "assistant",
            "content": "Hi — we need to buy USDT with cash and want to understand your process.",
        },
        {"role": "user", "content": "I can sell only with cash +1%"},
    ]
    out = _deterministic_draft(task, hist, None)
    body = out["draft_message"]
    low = body.lower()
    assert "Got it, cash and +1%" in body or "got it, cash and +1%" in low
    assert "where can we meet, and what cash currency" not in low
    assert "which payment method" not in low
    assert "closer to" not in low
    assert _question_mark_count(body) <= 2
    assert _operational_keyword_hits(body) == 0
