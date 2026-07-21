"""
Goal / collection classification for AI Agent (facts JSON only — no DB migration).

Drives when price counters are allowed vs. operational / process discovery drafts.
"""
from __future__ import annotations

import re
from typing import Any, Optional

# --- Inference ---

_PROCESS_GOAL = re.compile(
    r"get full information|full information|full details|how to do|how it works|\bprocess\b|"
    r"\boperational\b|map how|walk me through|need details|need information",
    re.I,
)
_AVAIL_GOAL = re.compile(
    r"\bavailable\b|in stock|\bdo you have\b|\bcan you provide\b|have you got",
    re.I,
)
_BANK_COLLECTION = re.compile(
    r"bank transfer|bank card|china bank|usd transfer|wire transfer|swift|iban|"
    r"receiver details|sender details",
    re.I,
)
_CARD_COLLECTION = re.compile(r"\bcard\b", re.I)
_BANK_WORD = re.compile(r"\bbank\b", re.I)
_CASH_COLLECTION = re.compile(
    r"\bcash\b|cash in|meet up|\bmeet\b|\blocation\b|yerevan|hand to hand|face to face",
    re.I,
)


def infer_goal_collection_modes(goal: str) -> dict[str, Any]:
    """
    Classify operator goal into ``goal_mode`` and optional ``collection_mode``.

    ``collection_mode`` is orthogonal (cash vs bank logistics) and can pair with
    ``process_discovery`` when the operator asks for full info and mentions cash.
    """
    g = (goal or "").strip()
    low = g.lower()
    out: dict[str, Any] = {"goal_mode": "deal_negotiation", "collection_mode": None}

    if _PROCESS_GOAL.search(low):
        out["goal_mode"] = "process_discovery"
    elif _AVAIL_GOAL.search(low):
        out["goal_mode"] = "availability_check"

    if _BANK_COLLECTION.search(low) or (_CARD_COLLECTION.search(low) and _BANK_WORD.search(low)):
        out["collection_mode"] = "bank_transfer_discovery"
    elif _CASH_COLLECTION.search(low):
        out["collection_mode"] = "cash_exchange_discovery"

    return out


def infer_payment_method_from_goal(goal: str) -> Optional[str]:
    low = (goal or "").lower()
    if re.search(r"\bcash\b", low):
        return "cash"
    return None


def infer_payment_method_from_counterparty(text: str) -> Optional[str]:
    low = (text or "").lower()
    if re.search(r"only cash|cash only|with cash|\bcash\s*\+|\bsell only with cash\b", low):
        return "cash"
    return None


def operational_discovery_active(facts: dict[str, Any]) -> bool:
    gm = str(facts.get("goal_mode") or "deal_negotiation").strip().lower()
    cm = str(facts.get("collection_mode") or "").strip().lower()
    if gm in ("process_discovery", "availability_check"):
        return True
    if cm in ("cash_exchange_discovery", "bank_transfer_discovery"):
        return True
    return False


def price_negotiation_counters_allowed(facts: dict[str, Any]) -> bool:
    """
    Aggressive OTC counters only when we are in deal mode with size and settlement context.
    """
    if str(facts.get("goal_mode") or "deal_negotiation").strip().lower() != "deal_negotiation":
        return False
    if facts.get("amount_crypto") is None:
        return False
    cm = str(facts.get("collection_mode") or "").strip().lower()
    if cm in ("cash_exchange_discovery", "bank_transfer_discovery"):
        return False
    return True


def apply_price_negotiation_gate(facts: dict[str, Any]) -> None:
    """Strip counter ladder when discovery / missing amount / bank-cash collection paths apply."""
    if price_negotiation_counters_allowed(facts):
        return
    act = facts.get("profit_action")
    if act in ("counter", "strong_counter"):
        facts["profit_action"] = "collect_process"
    facts.pop("next_counter_rate", None)
    facts.pop("counter_offer_rate", None)
    facts["final_best_requested"] = False
