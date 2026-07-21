"""Goal text → extracted facts (amount / network) regression tests."""
from __future__ import annotations

from src.ai_agent.ai_client import _infer_facts_from_goal


def test_erc20_digits_not_merged_into_amount():
    g = _infer_facts_from_goal("bay usdt erc20 2000 usdt")
    assert g.get("amount_crypto") == 2000.0
    assert g.get("payment_network") == "ERC20"
    assert g.get("side") == "buy"
    assert g.get("asset") == "USDT"


def test_trc20_after_amount():
    g = _infer_facts_from_goal("buy 1000 usdt trc20")
    assert g.get("amount_crypto") == 1000.0
    assert g.get("payment_network") == "TRC20"


def test_erc20_before_usdt_amount():
    g = _infer_facts_from_goal("erc20 usdt 2000")
    assert g.get("amount_crypto") == 2000.0
    assert g.get("payment_network") == "ERC20"


def test_humanize_maps_account_not_allowed():
    from src.ai_agent.public_errors import humanize_ai_agent_error

    s = humanize_ai_agent_error({"error": "account_not_allowed_for_ai_agent"})
    assert "110" in s or "AI Agent" in s
