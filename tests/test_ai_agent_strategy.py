"""Strategy merge: policy snapshot, tiers, operator-ready."""
from __future__ import annotations

from config.settings import settings

from src.ai_agent.strategy import build_otc_policy_from_settings, merge_profit_strategy


def test_merge_includes_otc_policy_and_rates():
    pol = build_otc_policy_from_settings(settings)
    m = merge_profit_strategy(
        {"side": "buy", "asset": "USDT", "goal_text": "buy 500 USDT"},
        policy=pol,
        latest_inbound_text="",
    )
    assert isinstance(m.get("otc_policy"), dict)
    assert m.get("target_rate")
    assert m.get("walkaway_rate")
    assert m.get("deal_size_tier") == "small"


def test_merge_parses_inbound_premium():
    pol = build_otc_policy_from_settings(settings)
    m = merge_profit_strategy(
        {"side": "buy", "asset": "USDT", "amount_crypto": 1000},
        policy=pol,
        latest_inbound_text="we can do +0.6%",
    )
    assert m.get("rate_premium_pct") == 0.6
    assert m.get("rate_evaluation") == "acceptable"
