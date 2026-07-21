"""Shared serializers for AI Agent HTTP responses (keeps routes ↔ service decoupled)."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

from src.ai_agent.conversation_state import is_polluted_message
from src.ai_agent.otc_profit import parse_premium_pct
from src.core.ai_agent_models import AiAgentAudit, AiAgentMessage, AiAgentTask


def _dt_iso(v: Any) -> Optional[str]:
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


def serialize_audit_row(a: AiAgentAudit) -> dict[str, Any]:
    return {
        "id": a.id,
        "action": a.action,
        "account_id": a.account_id,
        "detail": a.detail_json,
        "created_at": _dt_iso(a.created_at),
    }


def _serialize_auto_mode(t: AiAgentTask) -> str:
    raw = getattr(t, "auto_mode", None)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return "autonomous"
    return str(raw).strip().lower()


def _serialize_auto_delay_sec(t: AiAgentTask) -> int:
    raw = getattr(t, "auto_delay_sec", None)
    try:
        n = int(raw) if raw is not None else 20
    except (TypeError, ValueError):
        n = 20
    if n < 10:
        return 20
    return n


def serialize_message(m: AiAgentMessage) -> dict[str, Any]:
    return {
        "id": m.id,
        "task_id": m.task_id,
        "status": m.status,
        "direction": m.direction,
        "body": m.body,
        "telegram_message_id": m.telegram_message_id,
        "meta_json": m.meta_json,
        "created_at": _dt_iso(m.created_at),
    }


_PROFIT_UI_KEYS = (
    "rate_evaluation",
    "target_rate",
    "walkaway_rate",
    "counter_offer_rate",
    "deal_size_tier",
    "profit_action",
    "negotiation_stage",
    "strategy_phase",
    "suggested_next_action",
    "concession_count",
    "last_counter_rate",
    "next_counter_rate",
    "final_best_requested",
)


def serialize_profit_facts_for_ui(facts: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Subset of extracted facts for dashboard cards (no raw secrets)."""
    if not facts:
        return {}
    out: dict[str, Any] = {}
    for k in _PROFIT_UI_KEYS:
        if k in facts and facts[k] is not None and facts[k] != "":
            out[k] = facts[k]
    rev = facts.get("rate_evaluation")
    if rev in ("bad", "operator_review"):
        out["operator_decision_needed"] = True
    elif facts.get("suggested_next_action") == "operator_review":
        out["operator_decision_needed"] = True
    return out


_STATUS_LABELS: dict[str, str] = {
    "draft": "New",
    "waiting_admin_approval": "Draft ready",
    "waiting_reply": "Waiting reply",
    "ready_for_operator": "Ready",
    "risky": "Risk review",
    "paused": "Paused",
    "completed": "Completed",
    "failed": "Failed",
    "cancelled": "Cancelled",
}


def _merge_fact_sources(
    facts: Optional[dict[str, Any]], profit_facts: Optional[dict[str, Any]]
) -> dict[str, Any]:
    """Facts win over profit snapshot except empty keys are filled from profit."""
    m: dict[str, Any] = {}
    pf = profit_facts if isinstance(profit_facts, dict) else {}
    fd = facts if isinstance(facts, dict) else {}
    for k, v in pf.items():
        if v not in (None, ""):
            m[k] = v
    for k, v in fd.items():
        if v not in (None, ""):
            m[k] = v
    return m


def _effective_negotiation_stage(task: AiAgentTask, facts: Optional[dict[str, Any]]) -> str:
    fs = (facts or {}).get("negotiation_stage")
    if isinstance(fs, str) and fs.strip():
        return fs.strip().lower()
    return str(getattr(task, "negotiation_stage", None) or "opening").strip().lower()


def compute_ui_is_operator_ready(task: AiAgentTask, facts: Optional[dict[str, Any]]) -> bool:
    st = str(task.status or "").strip().lower()
    if st in ("completed", "failed", "cancelled"):
        return False
    return _effective_negotiation_stage(task, facts) == "ready_for_operator"


def compute_ui_filter_status(task: AiAgentTask, facts: Optional[dict[str, Any]]) -> str:
    """Bucket for dashboard filters (maps operator-ready rows to ``ready_for_operator``)."""
    st = str(task.status or "").strip().lower()
    if st in ("completed", "failed", "cancelled", "paused"):
        return st
    if compute_ui_is_operator_ready(task, facts):
        return "ready_for_operator"
    return st


def compute_ui_status_label(task: AiAgentTask, facts: Optional[dict[str, Any]]) -> str:
    st = str(task.status or "").strip().lower()
    if st in ("completed", "failed", "cancelled"):
        return _STATUS_LABELS.get(st, st or "—")
    if compute_ui_is_operator_ready(task, facts):
        return "Ready"
    if st == "risky" or _effective_negotiation_stage(task, facts) == "risky":
        return "Risk review"
    return _STATUS_LABELS.get(st, st or "—")


def compute_ui_next_action(task: AiAgentTask, facts: Optional[dict[str, Any]]) -> str:
    st = str(task.status or "").strip().lower()
    ns = _effective_negotiation_stage(task, facts)
    if st == "completed":
        return "Done"
    if st == "paused":
        return "Resume when ready"
    if compute_ui_is_operator_ready(task, facts):
        return "Review deal"
    if st == "risky" or ns == "risky":
        return "Risk review"
    if st == "waiting_reply":
        return "Wait for reply"
    if st == "waiting_admin_approval":
        return "Send draft"
    if st == "draft":
        return "Start agent"
    return "Continue"


def compute_ui_primary_action(task: AiAgentTask, facts: Optional[dict[str, Any]]) -> str:
    """Machine token for the single primary operator control (dashboard maps to labels)."""
    st = str(task.status or "").strip().lower()
    if st in ("completed", "failed", "cancelled"):
        return "none"
    if st == "paused":
        return "resume"
    if compute_ui_is_operator_ready(task, facts):
        return "review_deal"
    if st == "waiting_reply":
        return "sync"
    if st == "waiting_admin_approval":
        return "send"
    if st == "draft":
        return "start"
    if st == "risky":
        return "sync"
    return "start"


def _format_rate_for_ui(val: Any) -> Optional[str]:
    if val is None or val == "":
        return None
    try:
        x = float(val)
    except (TypeError, ValueError):
        s = str(val).strip()
        return s if s else None
    sign = "+" if x >= 0 else ""
    s = f"{sign}{x:.4f}".rstrip("0").rstrip(".")
    return s + "%"


def _effective_rate_premium(merged: dict[str, Any]) -> Optional[float]:
    rp = merged.get("rate_premium_pct")
    if rp is not None and str(rp).strip() != "":
        p = parse_premium_pct(rp)
        if p is not None:
            return p
        try:
            return float(rp)
        except (TypeError, ValueError):
            pass
    for k in ("counter_offer_rate", "next_counter_rate", "last_counter_rate", "target_rate"):
        p = parse_premium_pct(merged.get(k))
        if p is not None:
            return p
    return None


def _payment_line(merged: dict[str, Any]) -> Optional[str]:
    pm = str(merged.get("payment_method") or "").strip().lower()
    st = str(merged.get("settlement_type") or "").strip().lower()
    cm = str(merged.get("collection_mode") or "").strip().lower()
    if pm == "cash" or st == "cash" or "cash" in cm:
        return "Cash deal"
    if pm:
        return pm.replace("_", " ").title()
    if st:
        return st.title()
    return None


def build_ui_deal_briefing_rows(merged: dict[str, Any]) -> list[dict[str, str]]:
    """Operator briefing: only rows with real values (no placeholders). Each row has slug + label."""
    rows: list[dict[str, str]] = []

    side_s = merged.get("side")
    side = side_s.strip().title() if isinstance(side_s, str) and side_s.strip() else ""
    asset_s = merged.get("asset")
    asset = asset_s.strip().upper() if isinstance(asset_s, str) and asset_s.strip() else ""

    if side and asset:
        rows.append({"slug": "instrument", "label": "Deal", "value": f"{side} {asset}"})
    elif side:
        rows.append({"slug": "side", "label": "Side", "value": side})
    elif asset:
        rows.append({"slug": "asset", "label": "Asset", "value": asset})

    amt = merged.get("amount_crypto")
    amt_s: Optional[str] = None
    if amt is not None:
        try:
            af = float(amt)
            amt_s = str(int(af)) if af == int(af) else str(af)
        except (TypeError, ValueError):
            amt_s = str(amt)
    if amt_s:
        au = asset or (str(merged.get("asset") or "USDT").strip().upper() or "USDT")
        rows.append({"slug": "amount", "label": "Amount", "value": f"{amt_s} {au}"})

    rp = _effective_rate_premium(merged)
    if rp is not None:
        fr = _format_rate_for_ui(rp)
        if fr:
            rows.append({"slug": "rate", "label": "Rate", "value": fr})

    pay = _payment_line(merged)
    if pay:
        rows.append({"slug": "payment", "label": "Payment", "value": pay})

    cur = merged.get("fiat_currency")
    if isinstance(cur, str) and cur.strip():
        rows.append({"slug": "currency", "label": "Currency", "value": cur.strip().upper()})

    loc = merged.get("location")
    if isinstance(loc, str) and loc.strip():
        rows.append({"slug": "location", "label": "Location", "value": loc.strip()[:200]})

    tim = merged.get("meeting_time") or merged.get("payment_timing")
    if isinstance(tim, str) and tim.strip():
        rows.append({"slug": "time", "label": "Time", "value": tim.strip()[:120]})

    lim = merged.get("transaction_limit")
    if isinstance(lim, str) and lim.strip():
        rows.append({"slug": "limit", "label": "Limit", "value": lim.strip()[:160]})

    kyc = merged.get("kyc_required")
    if kyc is True:
        rows.append({"slug": "kyc", "label": "KYC", "value": "Passport/photo required"})

    risks = merged.get("risk_flags")
    if isinstance(risks, list) and risks:
        rs = "; ".join(str(x) for x in risks[:4])
        if len(risks) > 4:
            rs += "…"
        rows.append({"slug": "risk", "label": "Risk", "value": rs})
    elif str(merged.get("negotiation_stage") or "").lower() == "risky":
        rows.append({"slug": "risk", "label": "Risk", "value": "Flagged for review"})

    return rows


def compute_ui_deal_completion_pct(merged: dict[str, Any]) -> int:
    """Weighted completion 0–100 (operational fields weighted higher)."""
    weights: list[tuple[str, float, Any]] = [
        ("side", 9.0, merged.get("side")),
        ("asset", 9.0, merged.get("asset")),
        ("amount_or_limit", 11.0, merged.get("amount_crypto") or merged.get("transaction_limit")),
        ("rate", 11.0, _effective_rate_premium(merged)),
        ("payment", 10.0, _payment_line(merged)),
        ("currency", 7.0, merged.get("fiat_currency")),
        ("location", 8.0, merged.get("location")),
        ("time", 7.0, merged.get("meeting_time") or merged.get("payment_timing")),
        ("kyc", 8.0, merged.get("kyc_required")),
    ]
    total = sum(w for _, w, _ in weights)
    got = 0.0
    for _, w, v in weights:
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        if isinstance(v, bool):
            got += w
        elif isinstance(v, (int, float)):
            got += w
        else:
            got += w
    if total <= 0:
        return 0
    return int(round(min(100.0, max(0.0, got * 100.0 / total))))


_RE_PRESSURE = re.compile(
    r"(?i)\b(urgent|hurry|asap|last chance|now only|today only|send first|trust me)\b"
)
_RE_ESCROW = re.compile(r"(?i)\b(escrow|middleman|hold (?:the )?funds|third party)\b")
_RE_THIRD_WALLET = re.compile(
    r"(?i)\b(third[- ]party wallet|friend'?s wallet|not my wallet|different wallet|their wallet)\b"
)
_RE_NO_KYC = re.compile(r"(?i)\b(no kyc|without kyc|refuse(?:s|d)? (?:to )?(?:show )?id|anon(ymous)?|no id)\b")
_RE_SCAM = re.compile(r"(?i)\b(guaranteed profit|double your|recovery (?:service|wallet))\b")
_RE_INTERNAL_TIMELINE = re.compile(
    r"(?i)^\s*(?:\[auto\]|auto[- ]loop|diagnostic|system:|internal runtime|tool result)\b"
)


def compute_ui_risk_level(
    task: AiAgentTask, merged: dict[str, Any], facts: Optional[dict[str, Any]]
) -> str:
    ns = _effective_negotiation_stage(task, facts)
    st = str(task.status or "").strip().lower()
    risks = merged.get("risk_flags")
    has_risk_flags = isinstance(risks, list) and len(risks) > 0
    blob = " ".join(
        str(x)
        for x in (
            merged.get("deal_summary"),
            task.goal_text,
        )
        if x
    )
    inbound_blob = ""
    # merged may not have messages; risk from facts only if snippets exist
    if isinstance(facts, dict):
        inbound_blob += str(facts.get("last_counterparty_text") or "")

    score = 0
    if st == "risky" or ns == "risky":
        score += 4
    if has_risk_flags:
        score += 4
    if merged.get("rate_evaluation") in ("bad", "operator_review") or merged.get("operator_decision_needed"):
        score += 2
    rp = _effective_rate_premium(merged)
    if rp is not None and abs(rp) >= 8:
        score += 2
    if rp is not None and merged.get("side") == "buy" and rp < -2:
        score += 1
    if merged.get("kyc_required") is False and (has_risk_flags or _RE_PRESSURE.search(blob or "")):
        score += 2
    if _RE_SCAM.search(blob or ""):
        score += 5
    if _RE_PRESSURE.search(blob or inbound_blob):
        score += 1
    if _RE_ESCROW.search(blob or inbound_blob):
        score += 2
    if _RE_THIRD_WALLET.search(blob or inbound_blob):
        score += 3
    if _RE_NO_KYC.search(blob or inbound_blob):
        score += 2

    if score >= 7:
        return "critical"
    if score >= 5:
        return "high"
    if score >= 2:
        return "medium"
    return "low"


def compute_ui_escalation_reason(
    task: AiAgentTask, merged: dict[str, Any], facts: Optional[dict[str, Any]]
) -> Optional[str]:
    st = str(task.status or "").strip().lower()
    ns = _effective_negotiation_stage(task, facts)
    risk = compute_ui_risk_level(task, merged, facts)
    ready = compute_ui_is_operator_ready(task, facts)

    if not ready:
        if merged.get("suggested_next_action") == "operator_review" or merged.get("rate_evaluation") in (
            "bad",
            "operator_review",
        ):
            return "Spread exceeds target."
        if st == "risky" or ns == "risky":
            return "Potential scam indicators."
        if risk in ("high", "critical"):
            return "Potential scam indicators."
        return None

    pct = compute_ui_deal_completion_pct(merged)
    loc_ok = isinstance(merged.get("location"), str) and bool(str(merged.get("location")).strip())
    time_ok = bool(
        (isinstance(merged.get("meeting_time"), str) and merged.get("meeting_time", "").strip())
        or (isinstance(merged.get("payment_timing"), str) and merged.get("payment_timing", "").strip())
    )
    if pct >= 75 and loc_ok and time_ok:
        return "Meeting details confirmed."
    if pct >= 75:
        return "All operational details collected."
    if merged.get("kyc_required") is False:
        return "Counterparty refused verification."
    if merged.get("rate_evaluation") in ("bad", "operator_review"):
        return "Spread exceeds target."
    risks = merged.get("risk_flags")
    if isinstance(risks, list) and risks:
        return "Potential scam indicators."
    return "Desk handoff — review snapshot and confirm."


def compute_ui_recommended_action(
    task: AiAgentTask,
    merged: dict[str, Any],
    facts: Optional[dict[str, Any]],
    profit_facts: Optional[dict[str, Any]],
) -> str:
    st = str(task.status or "").strip().lower()
    ns = _effective_negotiation_stage(task, facts)
    pf = profit_facts if isinstance(profit_facts, dict) else {}
    act = str(pf.get("profit_action") or merged.get("profit_action") or "").lower()
    risk = compute_ui_risk_level(task, merged, facts)

    if compute_ui_is_operator_ready(task, facts):
        return "Proceed to operator review."
    if risk == "critical" or st == "risky" or ns == "risky":
        return "High-risk counterparty — manual review."
    if merged.get("suggested_next_action") == "operator_review" or merged.get("rate_evaluation") in (
        "bad",
        "operator_review",
    ):
        return "Desk review required before proceeding."
    if act in ("counter", "strong_counter"):
        return "Negotiate lower spread."
    if merged.get("kyc_required") is None and st not in ("completed",):
        return "Request payment verification."
    if merged.get("wallet_required") and risk in ("medium", "high", "critical"):
        return "Request wallet ownership proof."
    if st == "waiting_reply":
        return "Wait for seller response."
    if st == "waiting_admin_approval":
        return "Review draft and send when comfortable."
    if st == "draft":
        return "Sync the thread and generate the first desk message."
    return "Continue monitoring this negotiation."


def detect_ui_language_from_messages(messages: Optional[list[dict[str, Any]]]) -> str:
    parts: list[str] = []
    for row in messages or []:
        if not isinstance(row, dict):
            continue
        d = str(row.get("direction") or "").lower()
        if d not in ("in", "inbound", "user", "counterparty"):
            continue
        parts.append(str(row.get("body") or ""))
    joined = " ".join(parts)[:12000]
    if not joined.strip():
        return "EN"
    c_cyr = len(re.findall(r"[\u0400-\u04FF]", joined))
    c_arm = len(re.findall(r"[\u0530-\u058F]", joined))
    c_lat = len(re.findall(r"[A-Za-z]", joined))
    total = c_cyr + c_arm + c_lat
    if total < 6:
        if c_cyr and c_arm:
            return "MIXED"
        if c_cyr:
            return "RU"
        if c_arm:
            return "HY"
        return "EN"
    sc, sa, sl = c_cyr / total, c_arm / total, c_lat / total
    strong = sum(1 for x in (sc, sa, sl) if x >= 0.28)
    if strong >= 2:
        return "MIXED"
    if sc >= 0.55:
        return "RU"
    if sa >= 0.55:
        return "HY"
    if sl >= 0.55 and (sc >= 0.12 or sa >= 0.12):
        return "MIXED"
    return "EN"


def _message_role_dict(m: dict[str, Any]) -> dict[str, Any]:
    d = str(m.get("direction") or "").lower()
    role = "assistant" if d in ("out", "outbound", "assistant", "agent") else "user"
    return {"role": role, "content": str(m.get("body") or ""), "direction": d, "status": m.get("status")}


def build_ui_timeline_messages(messages: Optional[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Chat-style timeline: drop noise, de-dupe drafts, cap length."""
    if not messages:
        return []
    out: list[dict[str, Any]] = []
    last_draft_body: Optional[str] = None
    for m in messages:
        if not isinstance(m, dict):
            continue
        body = str(m.get("body") or "").strip()
        if not body:
            continue
        if is_polluted_message(_message_role_dict(m)):
            continue
        if _RE_INTERNAL_TIMELINE.search(body):
            continue
        d = str(m.get("direction") or "").lower()
        st = str(m.get("status") or "").lower()
        if d in ("out", "outbound", "assistant") and st == "draft":
            if body == last_draft_body:
                continue
            last_draft_body = body
            tag = "AI draft"
        elif d in ("out", "outbound", "assistant") and st == "sent":
            tag = "Sent"
            last_draft_body = None
        elif d in ("in", "inbound", "user", "counterparty"):
            tag = "Received"
            last_draft_body = None
        else:
            tag = "Operator"
            last_draft_body = None
        meta = m.get("meta_json") if isinstance(m.get("meta_json"), dict) else {}
        if isinstance(meta, dict) and meta.get("skip_timeline"):
            continue
        out.append(
            {
                "id": m.get("id"),
                "tag": tag,
                "body": body[:4000],
                "created_at": m.get("created_at"),
                "align": "end" if tag in ("AI draft", "Sent", "Operator") else "start",
            }
        )
    return out[-12:]


def _parse_iso_utc(s: Any) -> Optional[datetime]:
    if s is None:
        return None
    if isinstance(s, datetime):
        dt = s
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    raw = str(s).strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _relative_last_seen_hint(iso_s: Any) -> Optional[str]:
    dt = _parse_iso_utc(iso_s)
    if dt is None:
        return None
    now = datetime.now(timezone.utc)
    delta = now - dt
    sec = max(0, int(delta.total_seconds()))
    if sec < 90:
        return "moments ago"
    if sec < 3600:
        m = max(1, sec // 60)
        return f"{m} min ago"
    if sec < 86400:
        h = max(1, sec // 3600)
        return f"{h} h ago"
    if sec < 172800:
        return "yesterday"
    if sec < 604800:
        d = sec // 86400
        return f"{d} days ago"
    return dt.date().isoformat()


def build_ui_counterparty_memory(
    task: AiAgentTask,
    merged: dict[str, Any],
    messages: Optional[list[dict[str, Any]]],
    risk_level: str,
) -> dict[str, Any]:
    pm = _payment_line(merged)
    rp = _effective_rate_premium(merged)
    last_spread: Optional[str] = None
    if rp is not None:
        last_spread = _format_rate_for_ui(rp)

    lim = merged.get("transaction_limit")
    typical_limits = lim.strip()[:120] if isinstance(lim, str) and lim.strip() else None

    lang = detect_ui_language_from_messages(messages)

    last_in_ts = None
    for row in reversed(messages or []):
        if not isinstance(row, dict):
            continue
        d = str(row.get("direction") or "").lower()
        if d in ("in", "inbound", "user", "counterparty"):
            last_in_ts = row.get("created_at")
            break

    last_seen = _relative_last_seen_hint(last_in_ts)

    risks = merged.get("risk_flags")
    risk_history: Optional[str] = None
    if isinstance(risks, list) and risks:
        risk_history = "; ".join(str(x) for x in risks[:4])
        if len(risks) > 4:
            risk_history += "…"

    rh_list = risks if isinstance(risks, list) else []
    if risk_level in ("high", "critical"):
        trust_band = "problematic"
        trust_label = "Problematic"
    elif risk_level == "medium" or rh_list:
        trust_band = "unknown"
        trust_label = "Unknown"
    elif risk_level == "low" and merged.get("kyc_required") is True and not rh_list:
        trust_band = "trusted"
        trust_label = "Trusted"
    else:
        trust_band = "unknown"
        trust_label = "Unknown"

    archetype = "Cash seller" if pm and "cash" in pm.lower() else None

    lines: list[str] = []
    if archetype:
        lines.append(archetype)
    if last_spread:
        lines.append(f"Typical spread: {last_spread}")
    if pm:
        lines.append(f"Preferred payment: {pm}")
    lines.append(f"Language: {lang}")
    if typical_limits:
        lines.append(f"Typical limits: {typical_limits}")
    if risk_history:
        lines.append(f"Risk history: {risk_history}")
    if last_seen:
        lines.append(f"Last seen: {last_seen}")
    lines.append(f"Trust: {trust_label}")

    return {
        "seller_archetype": archetype,
        "last_spread_text": last_spread,
        "preferred_payment": pm,
        "preferred_language": lang,
        "typical_limits": typical_limits,
        "risk_history": risk_history,
        "last_seen_hint": last_seen,
        "trust_band": trust_band,
        "trust_label": trust_label,
        "lines": lines,
    }


def attach_task_ui_fields(
    out: dict[str, Any],
    task: AiAgentTask,
    *,
    facts: Optional[dict[str, Any]] = None,
    profit_facts: Optional[dict[str, Any]] = None,
    messages: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Adds operator-centric computed fields (no DB columns). Mutates and returns ``out``."""
    pf = profit_facts if isinstance(profit_facts, dict) else {}
    merged = _merge_fact_sources(facts, pf)
    risk = compute_ui_risk_level(task, merged, facts)

    out["ui_status_label"] = compute_ui_status_label(task, facts)
    out["ui_next_action"] = compute_ui_next_action(task, facts)
    out["ui_primary_action"] = compute_ui_primary_action(task, facts)
    out["ui_is_operator_ready"] = compute_ui_is_operator_ready(task, facts)
    briefing = build_ui_deal_briefing_rows(merged)
    out["ui_deal_summary_rows"] = briefing
    out["ui_deal_summary"] = {r["slug"]: r["value"] for r in briefing}
    esc = compute_ui_escalation_reason(task, merged, facts)
    out["ui_escalation_reason"] = esc
    out["ui_deal_completion_pct"] = compute_ui_deal_completion_pct(merged)
    out["ui_risk_level"] = risk
    out["ui_recommended_action"] = compute_ui_recommended_action(task, merged, facts, pf)
    out["ui_language_detected"] = detect_ui_language_from_messages(messages)
    out["ui_counterparty_memory"] = build_ui_counterparty_memory(task, merged, messages, risk)
    out["ui_timeline"] = build_ui_timeline_messages(messages)
    out["ui_list_summary"] = compute_ui_list_summary(task, facts)
    out["ui_show_missing_block"] = compute_ui_show_missing_block(task, facts)
    out["ui_filter_status"] = compute_ui_filter_status(task, facts)
    out["ui_has_profit_facts"] = bool(pf)
    return out


def compute_ui_list_summary(task: AiAgentTask, facts: Optional[dict[str, Any]]) -> str:
    f = facts if isinstance(facts, dict) else {}
    merged = _merge_fact_sources(f, {})
    parts: list[str] = []
    side = merged.get("side")
    asset = merged.get("asset")
    if side and asset:
        parts.append(f"{str(side).title()} {str(asset).upper()}")
    pay = _payment_line(merged)
    if pay:
        parts.append(pay)
    rp = _effective_rate_premium(merged)
    if rp is not None:
        fr = _format_rate_for_ui(rp)
        if fr:
            parts.append(fr)
    if parts:
        s = " · ".join(parts)
        return s if len(s) <= 96 else s[:93] + "…"
    ds = f.get("deal_summary")
    if isinstance(ds, str) and ds.strip():
        s = ds.strip().replace("\n", " ")
        return s if len(s) <= 96 else s[:93] + "…"
    g = (task.goal_text or "").strip()
    if not g:
        tgt = (getattr(task, "target_username_or_id", None) or "").strip()
        if tgt:
            return tgt[:96]
        return "New task"
    return g if len(g) <= 96 else g[:93] + "…"


def compute_ui_show_missing_block(task: AiAgentTask, facts: Optional[dict[str, Any]]) -> bool:
    return not compute_ui_is_operator_ready(task, facts)


def serialize_task_full(
    t: AiAgentTask,
    runtime: Optional[dict[str, Any]] = None,
    *,
    facts: Optional[dict[str, Any]] = None,
    profit_facts: Optional[dict[str, Any]] = None,
    messages: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": t.id,
        "account_id": t.account_id,
        "target": t.target_username_or_id,
        "goal": t.goal_text,
        "language": t.language,
        "tone": t.tone,
        "max_messages": t.max_messages,
        "status": t.status,
        "negotiation_stage": getattr(t, "negotiation_stage", None) or "opening",
        "auto_mode": _serialize_auto_mode(t),
        "auto_delay_sec": _serialize_auto_delay_sec(t),
        "auto_last_run_at": _dt_iso(getattr(t, "auto_last_run_at", None)),
        "final_summary": t.final_summary,
        "last_activity_at": _dt_iso(t.last_activity_at),
        "created_at": _dt_iso(t.created_at),
        "updated_at": _dt_iso(t.updated_at),
    }
    if runtime:
        for k, v in runtime.items():
            if str(k).startswith("agent_runtime") or k in ("account_ai_permitted",):
                out[k] = v
    attach_task_ui_fields(out, t, facts=facts, profit_facts=profit_facts, messages=messages)
    return out


# --- Legacy name used in tests ---
def build_ui_deal_summary(facts: Optional[dict[str, Any]]) -> dict[str, str]:
    rows = build_ui_deal_briefing_rows(_merge_fact_sources(facts, {}))
    return {r["slug"]: r["value"] for r in rows}
