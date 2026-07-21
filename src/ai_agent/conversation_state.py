"""
Stateful OTC operational memory: ingest seller text, resolve gaps, next short question.

Facts live in the same ``extracted_facts`` JSON as the rest of the AI Agent (no DB migration).
"""
from __future__ import annotations

import re
from typing import Any, Optional

from src.ai_agent.goal_modes import infer_payment_method_from_counterparty, operational_discovery_active
from src.ai_agent.otc_profit import parse_premium_pct

# --- Polluted / bootstrap assistant noise (do not feed state or OpenAI) ---

_POLLUTED_SUBSTRINGS = (
    "please confirm your",
    "please clarify",
    "kindly confirm",
    "verification process steps",
    "smooth settlement",
    "safety measures",
    "operational flow",
    "please outline limits, settlement flow",
)


def is_polluted_message(row: Any) -> bool:
    """Heuristic: strip assistant boilerplate / questionnaire spam from history."""
    if not isinstance(row, dict):
        return True
    role = str(row.get("role") or row.get("direction") or "").lower()
    if role in ("user", "in", "inbound", "counterparty"):
        return False
    if role not in ("assistant", "out", "outbound", "agent"):
        return False
    body = str(row.get("content") or row.get("body") or "").strip()
    if not body:
        return True
    if len(body) > 4000:
        return True
    low = body.lower()
    if any(s in low for s in _POLLUTED_SUBSTRINGS):
        return True
    if body.count("?") >= 4:
        return True
    return False


def filter_conversation_history(history: list[Any]) -> list[dict[str, Any]]:
    """Drop polluted assistant rows; collapse duplicate consecutive assistant bodies."""
    out: list[dict[str, Any]] = []
    last_asst_norm: Optional[str] = None
    for row in history or []:
        if not isinstance(row, dict):
            continue
        role = str(row.get("role") or row.get("direction") or "").lower()
        body = str(row.get("content") or row.get("body") or "")
        if role in ("assistant", "out", "outbound", "agent"):
            if is_polluted_message(row):
                continue
            norm = " ".join(body.lower().split())
            if norm and norm == last_asst_norm:
                continue
            last_asst_norm = norm if norm else last_asst_norm
        out.append(dict(row))
    return out


def _inbound_texts(history: list[Any]) -> list[str]:
    texts: list[str] = []
    for row in history or []:
        if not isinstance(row, dict):
            continue
        role = str(row.get("role") or row.get("direction") or "").lower()
        if role in ("user", "in", "inbound", "counterparty"):
            t = str(row.get("content") or row.get("body") or "").strip()
            if t:
                texts.append(t)
    return texts


def _last_inbound(history: list[Any]) -> str:
    tx = _inbound_texts(history)
    return tx[-1] if tx else ""


def _set_if_empty(facts: dict[str, Any], key: str, val: Any) -> None:
    if val is None or val == "":
        return
    cur = facts.get(key)
    if cur is None or (isinstance(cur, str) and not str(cur).strip()):
        facts[key] = val


def extract_operational_signals_from_text(text: str) -> dict[str, Any]:
    """Lightweight regex/keyword extraction from one seller message (no LLM)."""
    if not (text or "").strip():
        return {}
    low = text.lower()
    out: dict[str, Any] = {}

    pm = infer_payment_method_from_counterparty(text)
    if pm:
        out["payment_method"] = pm
    if "only cash" in low or "cash only" in low or re.search(r"\bcash\s*\+", low):
        out["payment_method"] = "cash"
        out["settlement_type"] = "cash"

    lm = re.search(
        r"(?i)(?:up to|max|maximum|at least|min|minimum|from)\s*([\d\s.,]+)\s*(?:k|m|usd|usdt)?",
        text,
    )
    if lm:
        raw = lm.group(1).replace(" ", "").replace(",", "")
        if raw.isdigit() or (raw and raw[0].isdigit()):
            out["transaction_limit"] = lm.group(0).strip()[:120]
    if not out.get("transaction_limit"):
        lm2 = re.search(
            r"(?i)\blimit\b.{0,140}?(\d[\d\s.,]*)\s*(?:usdt|usd)\b",
            text,
        )
        if lm2:
            raw2 = lm2.group(1).replace(" ", "").replace(",", "")
            if raw2.isdigit() or (raw2 and raw2[0].isdigit()):
                out["transaction_limit"] = lm2.group(0).strip()[:120]

    p = parse_premium_pct(text)
    if p is not None:
        out["rate_premium_pct"] = p

    if re.search(r"\b(i|we)\s+(can\s+)?sell\b", low) or "seller" in low:
        out["counterparty_role"] = "seller"
    if re.search(r"\b(i|we)\s+(can\s+)?buy\b", low) or "buyer" in low:
        out["counterparty_role"] = "buyer"

    for cur in ("amd", "usd", "rub", "eur"):
        if re.search(rf"\b{cur}\b", low):
            if cur == "amd":
                out["fiat_currency"] = "AMD"
            elif cur == "usd":
                out["fiat_currency"] = "USD"
            elif cur == "rub":
                out["fiat_currency"] = "RUB"
            else:
                out["fiat_currency"] = "EUR"
            break
    if "dram" in low:
        out["fiat_currency"] = "AMD"

    mloc = re.search(
        r"(?i)(yerevan|kentron|arabkir|nor\s+nork|arshakun\w*|komitas|buzand|masis)\b[^\n.?!]{0,120}",
        text,
    )
    if mloc:
        out["location"] = mloc.group(0).strip()[:200]
    mst = re.search(
        r"(?i)\b\d{1,3}\s+[a-z]{3,}(?:\s+(?:str|st\.?|street|poghots))?\s*,?\s*\d{1,4}[a-z]?\b",
        text,
    )
    if mst and not out.get("location"):
        out["location"] = mst.group(0).strip()[:200]

    mt = re.search(
        r"\b(\d{1,2}:\d{2})\b|\b(\d{1,2})\s*(am|pm)\b|evening|tomorrow|today\s+after|noon",
        low,
    )
    if mt:
        out["meeting_time"] = mt.group(0).strip()[:80]

    if "no kyc" in low or "without kyc" in low:
        out["kyc_required"] = False
    elif re.search(r"\bkyc\b|\bpassport\b|\bid\b|\bverification\b", low):
        out["kyc_required"] = True

    am = re.search(r"(?i)\b(\d+)\s*k\b", low)
    if am:
        try:
            out["amount_crypto"] = float(am.group(1)) * 1000.0
            out["volume_confirmed"] = True
        except ValueError:
            pass

    if re.search(r"\btrc\s*-?\s*20\b|trc20", low):
        out["wallet_required"] = "TRC20"
    elif re.search(r"\berc\s*-?\s*20\b|erc20", low):
        out["wallet_required"] = "ERC20"

    if re.search(r"\bchina\b|\buk\b|\busa\b|\bua\b|\barmenia\b", low):
        m = re.search(r"(?i)\b(china|uk|usa|ua|armenia)\b", low)
        if m:
            out["card_country"] = m.group(1).title()

    if re.search(r"\bbank\b", low) and not out.get("bank_name"):
        bm = re.search(r"(?i)bank[:\s]+([a-z][a-z\s]{2,30})", text)
        if bm:
            out["bank_name"] = bm.group(1).strip()[:80]

    return out


def ingest_operational_from_history(facts: dict[str, Any], history: list[Any]) -> None:
    """Merge signals from all inbound lines into ``facts`` (later messages override)."""
    for t in _inbound_texts(history):
        patch = extract_operational_signals_from_text(t)
        for k, v in patch.items():
            if v is not None and v != "":
                facts[k] = v


def _truthy(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, bool):
        return True
    if isinstance(v, (int, float)) and v == 0:
        return False
    return bool(str(v).strip())


def resolve_missing_operational_fields(
    facts: dict[str, Any],
    history: list[Any],
) -> dict[str, Any]:
    """
    Returns ``known`` (subset of resolved fields) and ``missing`` (ordered follow-ups).

    Caller must have run ``ingest_operational_from_history`` so ``facts`` reflects history.
    """
    cm = str(facts.get("collection_mode") or "").strip().lower()
    gm = str(facts.get("goal_mode") or "").strip().lower()
    known: dict[str, Any] = {}
    for key in (
        "location",
        "meeting_time",
        "payment_method",
        "fiat_currency",
        "kyc_required",
        "transaction_limit",
        "settlement_type",
        "wallet_required",
        "card_country",
        "bank_name",
        "bank_currency",
        "transfer_time",
        "counterparty_role",
        "volume_confirmed",
        "amount_crypto",
        "rate_premium_pct",
    ):
        v = facts.get(key)
        if key == "volume_confirmed":
            if facts.get("amount_crypto") is not None:
                known[key] = True
            continue
        if key == "amount_crypto" and facts.get("amount_crypto") is not None:
            known[key] = facts.get("amount_crypto")
            continue
        if _truthy(v) or (key == "kyc_required" and v is False):
            known[key] = v

    if cm == "bank_transfer_discovery":
        order = [
            "rate_premium_pct",
            "card_country",
            "bank_name",
            "bank_currency",
            "transaction_limit",
            "transfer_time",
            "kyc_required",
            "volume_confirmed",
        ]
        if gm != "process_discovery":
            order.append("counterparty_role")
    else:
        order = [
            "rate_premium_pct",
            "payment_method",
            "fiat_currency",
            "location",
            "meeting_time",
            "counterparty_role",
            "transaction_limit",
            "kyc_required",
            "wallet_required",
            "volume_confirmed",
        ]

    missing: list[str] = []
    for key in order:
        if key == "volume_confirmed":
            if facts.get("amount_crypto") is None and not facts.get("transaction_limit"):
                missing.append(key)
            continue
        if key == "kyc_required":
            if facts.get("kyc_required") is None:
                missing.append(key)
            continue
        if not _truthy(facts.get(key)):
            missing.append(key)

    return {"known": known, "missing": missing}


_BANNED_OUTPUT = (
    "please confirm",
    "kindly confirm",
    "please clarify",
    "verification process",
    "smooth settlement",
    "safety measures",
    "operational flow",
)


def _fmt_rate_pct(val: Any) -> str:
    try:
        pf = float(val)
        sign = "+" if pf >= 0 else ""
        return f"{sign}{pf:.2f}".rstrip("0").rstrip(".") + "%"
    except (TypeError, ValueError):
        return str(val or "").strip()


def _sanitize_trader_line(s: str) -> str:
    low = s.lower()
    for b in _BANNED_OUTPUT:
        if b in low:
            low = low.replace(b, "").replace("  ", " ")
    s = s.strip()
    parts = [p.strip() for p in s.split("\n\n") if p.strip()]
    s = "\n\n".join(parts[:2])
    sentences = re.split(r"(?<=[.!?])\s+", s)
    s = " ".join(sentences[:2]).strip()
    return s


def build_next_operational_question(
    facts: dict[str, Any],
    history: list[Any],
    goal: str = "",
) -> str:
    """
    One short trader reply: optional 1-line ack + max 1–2 targeted questions for the next gap.
    """
    cm = str(facts.get("collection_mode") or "").strip().lower()
    gm = str(facts.get("goal_mode") or "").strip().lower()
    missing = resolve_missing_operational_fields(facts, history)["missing"]
    if not missing:
        if operational_discovery_active(facts) and maybe_apply_operator_ready(facts):
            return _sanitize_trader_line(
                "Got the full picture on my side. Pinging the desk to lock this in — "
                "you'll hear back shortly."
            )
        return ""

    first = missing[0]
    second = missing[1] if len(missing) > 1 else None

    prem = facts.get("rate_premium_pct")
    pm = str(facts.get("payment_method") or "").strip().lower()
    ack = ""
    if prem is not None and pm == "cash" and first not in ("rate_premium_pct",):
        ack = f"Got it, cash and {_fmt_rate_pct(prem)}.\n\n"
    elif prem is not None and first not in ("rate_premium_pct",):
        ack = f"Got it, {_fmt_rate_pct(prem)}.\n\n"

    def q_for(k: str) -> str:
        if k == "counterparty_role":
            return "You're selling USDT to us on this leg, right?"
        if k == "rate_premium_pct":
            return "What rate are you working at all-in?"
        if k == "payment_method":
            return "You doing cash meet-ups or bank rails?"
        if k == "fiat_currency":
            return "AMD cash only, or USD bills too?"
        if k == "location":
            return "Where works for you — central Yerevan OK?"
        if k == "meeting_time":
            return "What time window suits you today or tomorrow?"
        if k == "transaction_limit":
            return "Rough max per meet at this rate?"
        if k == "kyc_required":
            return "ID in person or fine without?"
        if k == "wallet_required":
            return "On-chain leg needed or strictly hand cash?"
        if k == "volume_confirmed":
            return "How much USDT per ticket at this level?"
        if k == "card_country":
            return "Which country is the card from?"
        if k == "bank_name":
            return "Which bank exactly?"
        if k == "bank_currency":
            return "Account in USD or another currency?"
        if k == "transfer_time":
            return "How fast can you usually settle once agreed?"
        return ""

    parts_q: list[str] = []
    if (
        cm == "bank_transfer_discovery"
        and gm == "process_discovery"
        and "card_country" in missing
        and "bank_currency" in missing
    ):
        parts_q.append(
            "Which country/bank is the card from, and is it USD or another currency?"
        )
    else:
        for k in (first, second):
            if not k:
                continue
            q = q_for(k)
            if q:
                parts_q.append(q)
    if not parts_q:
        return ""
    body = ack + " ".join(parts_q[:2])
    if body.count("?") > 2:
        body = body.split("?")[0] + "?"
    return _sanitize_trader_line(body)


def maybe_apply_operator_ready(facts: dict[str, Any]) -> bool:
    """If discovery essentials are complete, mark ready_for_operator + summary."""
    if not operational_discovery_active(facts):
        return False
    cm = str(facts.get("collection_mode") or "").strip().lower()
    timing_ok = _truthy(facts.get("meeting_time")) or _truthy(facts.get("payment_timing"))
    transfer_ok = _truthy(facts.get("transfer_time")) or _truthy(facts.get("payment_timing"))

    if cm == "bank_transfer_discovery":
        if not all(_truthy(facts.get(k)) for k in ("rate_premium_pct", "card_country", "bank_currency")):
            return False
        if not transfer_ok:
            return False
    else:
        if not all(_truthy(facts.get(k)) for k in ("rate_premium_pct", "payment_method", "location")):
            return False
        if not timing_ok:
            return False

    lim_ok = _truthy(facts.get("transaction_limit")) or facts.get("amount_crypto") is not None
    if not lim_ok:
        return False
    if facts.get("kyc_required") is None:
        return False

    facts["negotiation_stage"] = "ready_for_operator"
    facts["deal_summary"] = (
        f"OTC snapshot: rate={facts.get('rate_premium_pct')}% "
        f"pay={facts.get('payment_method')} loc={str(facts.get('location') or '')[:80]} "
        f"time={facts.get('meeting_time') or facts.get('payment_timing')} "
        f"size={facts.get('transaction_limit') or facts.get('amount_crypto')} "
        f"kyc={facts.get('kyc_required')}"
    )[:500]
    return True
