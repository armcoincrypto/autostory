"""
OTC profit / premium evaluation (Phase 2).

Buy side: lower seller premium is better.
Sell side: higher buyer premium is better.
"""
from __future__ import annotations

import re
from typing import Any, Optional, TypedDict


class RateEvalBuy(TypedDict, total=False):
    rate_evaluation: str
    profit_action: str


class RateEvalSell(TypedDict, total=False):
    rate_evaluation: str
    profit_action: str


_LIMIT_RATE_WINDOW = re.compile(
    r"(?i)\b("
    r"max|maximum|limit|limits|per\s+transaction|per\s+meet|up\s+to|"
    r"min/|amount|volume|cash\s+transaction"
    r")\b"
)


def parse_premium_pct(value: Any) -> Optional[float]:
    """
    Parse desk premium / commission as a float percent (e.g. +1.0 for +1%).

    Amount and transaction-limit lines (e.g. ``Max 10000 usdt``) must NOT yield a rate:
    only explicit percent-style tokens and rate/commission wording count.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        x = float(value)
        # Mis-ingested USDT amounts have shown up here; real OTC premiums stay small.
        if abs(x) > 500:
            return None
        return x
    s = str(value).strip()
    if not s:
        return None

    norm = re.sub(r"(?i)\bpercent\b", "%", s)
    norm = norm.replace(",", ".")
    if "%" not in norm:
        return None

    def _from_groups(pattern: str) -> Optional[float]:
        m = re.search(pattern, norm)
        if not m:
            return None
        try:
            v = float(m.group(1))
        except (ValueError, IndexError):
            return None
        if abs(v) > 500:
            return None
        return v

    # Keyword-led phrases (authoritative)
    v = _from_groups(r"(?i)\b(?:premium|commission|fee)\s+([-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*%")
    if v is not None:
        return v
    v = _from_groups(r"(?i)\bplus\s+([-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*%")
    if v is not None:
        return v
    v = _from_groups(r"(?i)\bminus\s+([-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*%")
    if v is not None:
        return v
    v = _from_groups(r"(?i)\b(?:rate|cours)\s*[-+]?\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*%")
    if v is not None:
        return v

    # Generic ``+1.2%`` / ``-0.5%`` tokens; skip when clearly tied to size/limit wording
    # and the value looks like an amount, not a spread.
    chosen: Optional[float] = None
    for m in re.finditer(r"(?i)(?:^|(?<=[\s:;(,+]))([-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*%", norm):
        try:
            cand = float(m.group(1))
        except ValueError:
            continue
        if abs(cand) > 500:
            continue
        pre = norm[max(0, m.start() - 50) : m.start()].lower()
        # Do not treat huge %-looking figures after limit/size wording as OTC spread.
        if _LIMIT_RATE_WINDOW.search(pre) and abs(cand) >= 100:
            continue
        chosen = cand
    return chosen


def deal_size_tier(
    amount_crypto: Optional[float],
    *,
    large: float = 2000.0,
    small: float = 500.0,
) -> str:
    if amount_crypto is None:
        return "unknown"
    try:
        a = float(amount_crypto)
    except (TypeError, ValueError):
        return "unknown"
    if a >= large:
        return "large"
    if a <= small:
        return "small"
    return "medium"


def _buy_tiers(premium: float, target: float, max_buy: float) -> tuple[str, str]:
    """
    Map seller premium (buy side) to (rate_evaluation, profit_action).
    Bands above max_buy: negotiable, then bad, then operator_review.
    """
    nego_upper = max_buy + 1.0
    bad_upper = max_buy + 2.0
    if premium <= target:
        return "excellent", "accept"
    if premium <= max_buy:
        return "acceptable", "soft_negotiate"
    if premium <= nego_upper:
        return "negotiable", "counter"
    if premium <= bad_upper:
        return "bad", "strong_counter"
    return "operator_review", "operator_review"


def _sell_tiers(premium: float, target: float, min_sell: float) -> tuple[str, str]:
    """
    Buyer-offered premium when we sell: higher is better for us.
    """
    floor_nego = min_sell - 1.0
    floor_bad = min_sell - 2.0
    if premium >= target:
        return "excellent", "accept"
    if premium >= min_sell:
        return "acceptable", "negotiate_up"
    if premium >= floor_nego:
        return "negotiable", "counter"
    if premium >= floor_bad:
        return "bad", "strong_counter"
    return "operator_review", "operator_review"


def evaluate_buy_premium(
    seller_premium: Any,
    policy: dict[str, Any],
) -> RateEvalBuy:
    p = parse_premium_pct(seller_premium)
    if p is None:
        return {"rate_evaluation": "negotiable", "profit_action": "soft_negotiate"}
    target = float(policy.get("target_buy_premium_pct", 0.3))
    max_buy = float(policy.get("max_buy_premium_pct", 0.8))
    ev, act = _buy_tiers(p, target, max_buy)
    return {"rate_evaluation": ev, "profit_action": act}


def evaluate_sell_premium(
    buyer_premium: Any,
    policy: dict[str, Any],
) -> RateEvalSell:
    p = parse_premium_pct(buyer_premium)
    if p is None:
        return {"rate_evaluation": "negotiable", "profit_action": "soft_negotiate"}
    target = float(policy.get("target_sell_premium_pct", 1.0))
    min_sell = float(policy.get("min_sell_premium_pct", 0.4))
    ev, act = _sell_tiers(p, target, min_sell)
    return {"rate_evaluation": ev, "profit_action": act}
