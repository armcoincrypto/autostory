"""conversation_state: gaps, pollution filter, next-question engine."""
from __future__ import annotations

from src.ai_agent.conversation_state import (
    build_next_operational_question,
    filter_conversation_history,
    ingest_operational_from_history,
    is_polluted_message,
    resolve_missing_operational_fields,
)


def test_is_polluted_message_flags_questionnaire_spam():
    row = {
        "role": "assistant",
        "content": "Please clarify limits, verification process steps, and timing.",
    }
    assert is_polluted_message(row) is True


def test_filter_conversation_history_drops_polluted_and_duplicate_assistant():
    hist = [
        {"role": "user", "content": "only cash +1%"},
        {
            "role": "assistant",
            "content": "Please clarify limits, verification process steps, and timing.",
        },
        {"role": "assistant", "content": "Got it. Where meet?"},
        {"role": "assistant", "content": "Got it. Where meet?"},
    ]
    clean = filter_conversation_history(hist)
    assert len(clean) == 2
    bodies = [r.get("content") for r in clean if r.get("role") == "assistant"]
    assert "Please clarify" not in " ".join(bodies)


def test_resolve_missing_skips_known_cash_location_kyc():
    facts = {
        "goal_mode": "process_discovery",
        "collection_mode": "cash_exchange_discovery",
        "rate_premium_pct": 1.0,
        "payment_method": "cash",
        "location": "Yerevan Arshakunyac 40",
        "meeting_time": "10:00",
        "fiat_currency": "AMD",
        "kyc_required": False,
        "transaction_limit": "up to 50k",
    }
    ingest_operational_from_history(facts, [])
    r = resolve_missing_operational_fields(facts, [])
    assert "location" not in r["missing"]
    assert "payment_method" not in r["missing"]
    assert "kyc_required" not in r["missing"]
    assert "meeting_time" not in r["missing"]


def test_build_next_asks_only_volume_when_only_gap():
    facts = {
        "goal_mode": "process_discovery",
        "collection_mode": "cash_exchange_discovery",
        "rate_premium_pct": 1.0,
        "payment_method": "cash",
        "location": "Yerevan",
        "meeting_time": "10:00",
        "fiat_currency": "AMD",
        "kyc_required": False,
        "counterparty_role": "seller",
    }
    hist = [{"role": "user", "content": "cash +1% Yerevan 10am AMD no kyc"}]
    ingest_operational_from_history(facts, hist)
    q = build_next_operational_question(facts, hist, goal="")
    low = q.lower()
    assert "rough max per meet" in low or "how much usdt" in low
    assert low.count("?") <= 2


def test_memory_lock_only_cash_never_reasks_settlement_in_missing_order():
    facts = {
        "goal_mode": "process_discovery",
        "collection_mode": "cash_exchange_discovery",
        "rate_premium_pct": 0.5,
    }
    hist = [{"role": "user", "content": "Only cash"}]
    ingest_operational_from_history(facts, hist)
    assert facts.get("payment_method") == "cash"
    m = resolve_missing_operational_fields(facts, hist)["missing"]
    assert "payment_method" not in m


def test_extract_max_usdt_is_limit_not_rate():
    from src.ai_agent.conversation_state import extract_operational_signals_from_text

    o = extract_operational_signals_from_text("Max 10000 usdt")
    assert o.get("transaction_limit")
    assert "10000" in (o.get("transaction_limit") or "")
    assert o.get("rate_premium_pct") is None


def test_extract_limit_phrase_cash_transaction_usdt():
    from src.ai_agent.conversation_state import extract_operational_signals_from_text

    o = extract_operational_signals_from_text("There is limit for cash transaction 10000 usdt")
    assert o.get("transaction_limit")
    assert "10000" in (o.get("transaction_limit") or "")
    assert o.get("rate_premium_pct") is None


def test_ingest_history_task33_style_keeps_rate_after_limit_line():
    from src.ai_agent.conversation_state import ingest_operational_from_history

    facts: dict = {
        "goal_mode": "process_discovery",
        "collection_mode": "cash_exchange_discovery",
    }
    hist = [
        {"role": "user", "content": "I can sell only with cash +1%"},
        {"role": "user", "content": "Max 10000 usdt"},
    ]
    ingest_operational_from_history(facts, hist)
    assert facts.get("rate_premium_pct") == 1.0
    assert facts.get("transaction_limit")
    assert facts.get("rate_premium_pct") != 10000.0
