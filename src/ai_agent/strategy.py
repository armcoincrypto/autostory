"""
Negotiation strategy merges: OTC profit policy, concessions, operator-ready staging.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from config.settings import Settings, settings as default_settings

from src.ai_agent.goal_modes import (
    apply_price_negotiation_gate,
    infer_payment_method_from_counterparty,
)
from src.ai_agent.otc_profit import (
    deal_size_tier,
    evaluate_buy_premium,
    evaluate_sell_premium,
    parse_premium_pct,
)


def build_otc_policy_from_settings(s: Settings | None = None) -> dict[str, Any]:
    cfg = s or default_settings
    pol = {
        "base_market_rate_source": getattr(cfg, "ai_agent_otc_base_market_rate_source", "manual"),
        "target_buy_premium_pct": float(cfg.ai_agent_otc_target_buy_premium_pct),
        "max_buy_premium_pct": float(cfg.ai_agent_otc_max_buy_premium_pct),
        "target_sell_premium_pct": float(cfg.ai_agent_otc_target_sell_premium_pct),
        "min_sell_premium_pct": float(cfg.ai_agent_otc_min_sell_premium_pct),
        "large_deal_amount": float(cfg.ai_agent_otc_large_deal_amount),
        "small_deal_amount": float(cfg.ai_agent_otc_small_deal_amount),
    }
    return pol


def _fmt_rate_pct(p: float) -> str:
    sign = "+" if p >= 0 else ""
    return f"{sign}{p:.2f}".rstrip("0").rstrip(".") + "%"


def target_and_walkaway_for_facts(side: str, policy: dict[str, Any]) -> tuple[str, str]:
    s = (side or "").strip().lower()
    if s == "buy":
        t = float(policy["target_buy_premium_pct"])
        w = float(policy["max_buy_premium_pct"])
        return _fmt_rate_pct(t), _fmt_rate_pct(w)
    if s == "sell":
        t = float(policy["target_sell_premium_pct"])
        w = float(policy["min_sell_premium_pct"])
        return _fmt_rate_pct(t), _fmt_rate_pct(w)
    return "", ""


def extract_amount_from_goal(goal: str) -> Optional[float]:
    if not goal:
        return None
    m = re.search(
        r"(?i)(?:buy|sell|purchase|купить|продать)?\s*([\d][\d\s.,]*)\s*(?:usdt|usdт|btc|eth)",
        goal,
    )
    if not m:
        m = re.search(r"(?i)\b([\d][\d\s.,]*)\s*(?:usdt)\b", goal)
    if not m:
        return None
    raw = m.group(1).replace(" ", "").replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return None


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _next_buy_counter_rate(
    seller_premium: float,
    policy: dict[str, Any],
    concession_count: int,
    last_counter: Optional[float],
) -> tuple[Optional[float], bool]:
    """
    Returns (next_rate, final_best_requested).
    Max 2 concessions: first midpoint target..seller, second midpoint last..max.
    """
    target = float(policy["target_buy_premium_pct"])
    max_buy = float(policy["max_buy_premium_pct"])
    if concession_count <= 0:
        mid = (target + seller_premium) / 2.0
        return _clamp(mid, target, max_buy), False
    if concession_count == 1:
        base = last_counter if last_counter is not None else (target + seller_premium) / 2.0
        mid2 = (base + max_buy) / 2.0
        return _clamp(mid2, target, max_buy), False
    return None, True


def _next_sell_counter_rate(
    buyer_premium: float,
    policy: dict[str, Any],
    concession_count: int,
    last_counter: Optional[float],
) -> tuple[Optional[float], bool]:
    """Push buyer premium upward toward target."""
    target = float(policy["target_sell_premium_pct"])
    min_sell = float(policy["min_sell_premium_pct"])
    if concession_count <= 0:
        mid = (buyer_premium + target) / 2.0
        return _clamp(mid, buyer_premium, target), False
    if concession_count == 1:
        base = last_counter if last_counter is not None else (buyer_premium + target) / 2.0
        mid2 = (base + target) / 2.0
        return _clamp(mid2, min_sell, target), False
    return None, True


def merge_profit_strategy(
    base_facts: dict[str, Any],
    *,
    policy: dict[str, Any],
    latest_inbound_text: str = "",
) -> dict[str, Any]:
    """
    Mutates a copy of base_facts with otc_policy, tiers, rate evaluation, concessions,
    operator-ready hints. ``base_facts`` should already include side/asset from goal inference.
    """
    out = dict(base_facts)
    out["otc_policy"] = dict(policy)

    side = (out.get("side") or "").strip().lower()
    amt = out.get("amount_crypto")
    if amt is None and out.get("goal_text"):
        amt = extract_amount_from_goal(str(out.get("goal_text")))
        if amt is not None:
            out["amount_crypto"] = amt
    tier = deal_size_tier(
        float(amt) if amt is not None else None,
        large=float(policy["large_deal_amount"]),
        small=float(policy["small_deal_amount"]),
    )
    out["deal_size_tier"] = tier

    tr, wr = target_and_walkaway_for_facts(side, policy)
    out["target_rate"] = tr or None
    out["walkaway_rate"] = wr or None

    # Premium from facts or last inbound (drop corrupted numeric junk mis-filed as %).
    prem_raw = out.get("rate_premium_pct")
    if isinstance(prem_raw, (int, float)) and not isinstance(prem_raw, bool):
        if parse_premium_pct(prem_raw) is None:
            out.pop("rate_premium_pct", None)
            prem_raw = None
    if prem_raw is None and latest_inbound_text:
        pm = parse_premium_pct(latest_inbound_text)
        if pm is not None:
            prem_raw = pm
            out["rate_premium_pct"] = pm
    pmeth = infer_payment_method_from_counterparty(latest_inbound_text or "")
    if pmeth and not str(out.get("payment_method") or "").strip():
        out["payment_method"] = pmeth
    ev: dict[str, Any] = {}
    prem_for_eval = prem_raw if prem_raw is not None else latest_inbound_text
    sp_check = parse_premium_pct(prem_for_eval)
    if sp_check is None:
        out["rate_evaluation"] = None
        out["profit_action"] = None
    elif side == "buy":
        ev = evaluate_buy_premium(prem_for_eval, policy)
        out["rate_evaluation"] = ev.get("rate_evaluation")
        out["profit_action"] = ev.get("profit_action")
    elif side == "sell":
        ev = evaluate_sell_premium(prem_for_eval, policy)
        out["rate_evaluation"] = ev.get("rate_evaluation")
        out["profit_action"] = ev.get("profit_action")
    else:
        out["rate_evaluation"] = "negotiable"
        out["profit_action"] = "soft_negotiate"

    # Concession ladder (buy/sell)
    cc = int(out.get("concession_count") or 0)
    last_c = parse_premium_pct(out.get("last_counter_rate"))
    sp = sp_check
    final_best = bool(out.get("final_best_requested"))
    next_rate: Optional[float] = None
    if sp is not None and side == "buy" and out["rate_evaluation"] not in ("excellent", "acceptable"):
        if cc < 2:
            next_rate, final_best = _next_buy_counter_rate(sp, policy, cc, last_c)
        else:
            final_best = True
    if sp is not None and side == "sell" and (
        out["rate_evaluation"] in ("negotiable", "bad", "operator_review")
        or (out["rate_evaluation"] == "acceptable" and out.get("profit_action") == "negotiate_up")
    ):
        if cc < 2:
            next_rate, final_best = _next_sell_counter_rate(sp, policy, cc, last_c)
        else:
            final_best = True

    if next_rate is not None:
        out["next_counter_rate"] = _fmt_rate_pct(next_rate)
        out["counter_offer_rate"] = out["next_counter_rate"]
    else:
        out.pop("next_counter_rate", None)

    out["final_best_requested"] = bool(final_best)

    apply_operator_ready_fields(out, policy)
    apply_price_negotiation_gate(out)
    _apply_counter_negotiation_stage(out)
    return out


def _apply_counter_negotiation_stage(facts: dict[str, Any]) -> None:
    """When pushing on price, leave opening stage; do not override operator handoff."""
    if facts.get("negotiation_stage") == "ready_for_operator":
        return
    if facts.get("suggested_next_action") == "operator_review":
        return
    act = facts.get("profit_action") or ""
    if act not in ("counter", "strong_counter"):
        return
    facts["negotiation_stage"] = "negotiating_price"
    facts["strategy_phase"] = "push"


def _core_deal_complete(facts: dict[str, Any]) -> bool:
    if not facts.get("side") or not facts.get("asset"):
        return False
    if facts.get("amount_crypto") is None:
        return False
    if parse_premium_pct(facts.get("rate_premium_pct")) is None:
        return False
    return True


def _payment_complete(facts: dict[str, Any]) -> bool:
    return bool(
        (facts.get("payment_method") or "").strip()
        or (facts.get("payment_timing") or "").strip()
    )


def apply_operator_ready_fields(facts: dict[str, Any], policy: dict[str, Any]) -> None:
    """Set negotiation_stage / suggested_next_action / deal_summary operator lines."""
    rev = facts.get("rate_evaluation")
    payment_ok = _payment_complete(facts)
    core = _core_deal_complete(facts)

    facts.pop("suggested_next_action", None)
    prev_summary = str(facts.get("deal_summary") or "").strip()

    if not core:
        return

    max_buy = float(policy.get("max_buy_premium_pct", 0.8))
    min_sell = float(policy.get("min_sell_premium_pct", 0.4))
    side = (facts.get("side") or "").lower()
    p = parse_premium_pct(facts.get("rate_premium_pct"))

    bad_rate_line = ""
    if side == "buy" and p is not None and p > max_buy:
        bad_rate_line = "Rate is above configured max. Operator decision required."
    elif side == "sell" and p is not None and p < min_sell:
        bad_rate_line = "Rate is below configured minimum. Operator decision required."

    if rev in ("excellent", "acceptable") and payment_ok:
        facts["negotiation_stage"] = "ready_for_operator"
        facts["suggested_next_action"] = "execute_or_confirm"
        return

    if rev in ("bad", "operator_review") and bad_rate_line:
        facts["negotiation_stage"] = "ready_for_operator"
        facts["suggested_next_action"] = "operator_review"
        if bad_rate_line not in prev_summary:
            facts["deal_summary"] = (prev_summary + " " + bad_rate_line).strip()
        return

    # Negotiable / still collecting payment
    if facts.get("negotiation_stage") == "ready_for_operator" and not payment_ok and rev in (
        "excellent",
        "acceptable",
    ):
        facts["negotiation_stage"] = "payment"


def profit_guidance_for_prompt(facts: dict[str, Any]) -> str:
    """Short block for OpenAI system prompt."""
    side = facts.get("side") or "unknown"
    co = facts.get("counter_offer_rate") or facts.get("next_counter_rate")
    base = (
        f"OTC profit context: side={side}, rate_evaluation={facts.get('rate_evaluation')}, "
        f"profit_action={facts.get('profit_action')}, target_rate={facts.get('target_rate')}, "
        f"walkaway_rate={facts.get('walkaway_rate')}, counter_offer_rate={co}, "
        f"deal_size_tier={facts.get('deal_size_tier')}, negotiation_stage={facts.get('negotiation_stage')}.\n"
    )
    rules = (
        "PROFIT_ACTION (authoritative — follow exactly):\n"
        "- counter / strong_counter: Negotiate. You MUST include counter_offer_rate (or a tight band around it). "
        "Do NOT ask the counterparty to confirm or execute at THEIR quoted premium. "
        "Forbidden phrases: 'at this rate', 'confirm if you can provide', 'confirm you can provide' "
        "(unless you are clearly referring to counter_offer_rate, not the seller's rate).\n"
        "- accept: Acknowledge the agreed premium and advance to the next missing field (e.g. payment, timing).\n"
        "- soft_negotiate / negotiate_up: Nudge without locking in a bad premium; prefer one clear ask.\n"
        "- operator_review: Stop negotiating; give a neutral summary for a human operator.\n"
        "- collect_process: Do NOT push price. Acknowledge any quoted rate as information only. "
        "Ask the next missing operational / logistics questions (limits, location, timing, verification).\n"
    )
    gm = facts.get("goal_mode")
    cm = facts.get("collection_mode")
    if gm == "process_discovery" or cm == "cash_exchange_discovery":
        rules += (
            "\nPROCESS / CASH DISCOVERY MODE: goal_mode=%s collection_mode=%s. "
            "Treat rate as one fact among many — do NOT open with a counter-offer. "
            "If payment is already cash, never ask 'which payment method' — ask location, "
            "cash currency (AMD/USD/RUB), limits, verification, sequence, timing, safety.\n"
            % (gm, cm)
        )
    elif cm == "bank_transfer_discovery":
        rules += (
            "\nBANK / CARD DISCOVERY MODE: Ask country, bank name, account currency, "
            "receive vs send direction, limits, rate/commission, timing, required details, "
            "refund on failure — before negotiating price.\n"
        )
    return base + rules


def bump_concession_if_counter_sent(facts: dict[str, Any], sent_rate: Optional[float]) -> dict[str, Any]:
    """After proposing a numeric counter, persist concession ladder fields."""
    out = dict(facts)
    cc = int(out.get("concession_count") or 0)
    if sent_rate is not None:
        out["last_counter_rate"] = _fmt_rate_pct(sent_rate)
        if cc < 2:
            out["concession_count"] = cc + 1
    return out
