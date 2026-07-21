"""
P8.4 Dexpert Controller — read-only audit view (SystemLog–backed, no mutations).
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Optional

from flask import Blueprint, render_template, request
from flask_login import login_required
from sqlalchemy.orm import Session

from src.bot.kathleen.assistant_notifier import COMPONENT as READY_COMPONENT, MSG_NOTIFIED
from src.bot.kathleen.conversation_state import (
    COMPONENT as CONV_COMPONENT,
    LAST_PLAN_VALIDATION_ERRORS,
    MSG_CANCELLED as CONV_MSG_CANCELLED,
    MSG_COMPLETED,
    MSG_EXPIRED,
    MSG_PENDING,
    MSG_SUPERSEDED,
)
from src.bot.kathleen.plan_models import KathleenPlan
from src.bot.kathleen.plan_store import COMPONENT as PLAN_COMPONENT, plan_fingerprint
from src.core.database import get_db_context
from src.core.models import SystemLog

dexpert_audit_bp = Blueprint("dexpert_audit", __name__)


def _parse_iso(val: Any) -> Optional[datetime]:
    if not val:
        return None
    try:
        return datetime.fromisoformat(str(val).replace("Z", ""))
    except (TypeError, ValueError):
        return None


def _trunc(s: str, n: int = 72) -> str:
    t = (s or "").replace("\n", " ").strip()
    if len(t) <= n:
        return t or "—"
    return t[: n - 1] + "…"


def _conv_status_badge_class(message: str) -> str:
    return {
        MSG_PENDING: "warning",
        MSG_COMPLETED: "success",
        MSG_EXPIRED: "secondary",
        CONV_MSG_CANCELLED: "danger",
        MSG_SUPERSEDED: "info",
    }.get(message, "secondary")


def _plan_status_badge_class(message: str) -> str:
    if "pending" in message:
        return "warning"
    if "executed" in message:
        return "success"
    if "cancel" in message:
        return "danger"
    return "secondary"


KNOWN_CONV_MESSAGES = frozenset(
    {
        MSG_PENDING,
        MSG_COMPLETED,
        MSG_EXPIRED,
        CONV_MSG_CANCELLED,
        MSG_SUPERSEDED,
    }
)


def _safe_int(v: Any) -> Optional[int]:
    try:
        return int(v) if v is not None and str(v).strip() != "" else None
    except (TypeError, ValueError):
        return None


def _conv_status_match(row_message: str, filt: str) -> bool:
    if not filt:
        return True
    if filt in KNOWN_CONV_MESSAGES:
        return row_message == filt
    return filt.lower() in (row_message or "").lower()


def _fingerprint_from_collected(cf: dict[str, Any]) -> str:
    tgt = (cf.get("target_username") or "").strip()
    aid = cf.get("account_id")
    if aid is not None:
        try:
            return f"ai|{tgt}|{int(aid)}"
        except (TypeError, ValueError):
            return f"ai|{tgt}|?"
    return f"ai|{tgt}|auto"


def _plan_row_dict(row: SystemLog) -> dict[str, Any]:
    det = row.details or {}
    raw = det.get("plan")
    plan_id = f"K-{row.id}"
    ptype = "—"
    target = "—"
    account = "—"
    fp = "—"
    if isinstance(raw, dict):
        ptype = str(raw.get("plan_type") or "—")
        pl = raw.get("payload") or {}
        if isinstance(pl, dict):
            target = _trunc(
                str(pl.get("target") or pl.get("target_hint") or pl.get("source_hint") or "—"),
                48,
            )
            if pl.get("account_id") is not None:
                account = str(pl.get("account_id"))
            elif pl.get("n_accounts") is not None:
                account = f"n={pl.get('n_accounts')}"
        try:
            plan = KathleenPlan.from_dict(raw)
            fp = plan_fingerprint(plan)
        except Exception:
            fp = "—"
    return {
        "time": row.created_at,
        "plan_id": plan_id,
        "type": ptype,
        "target": target,
        "account": account,
        "status": row.message,
        "status_badge": _plan_status_badge_class(row.message),
        "fingerprint": fp,
        "dup_note": "—",
    }


def _load_audit_context(db: Session, args: Any) -> dict[str, Any]:
    since_h = int(args.get("since_hours") or 168)
    since_h = max(1, min(since_h, 24 * 90))
    cutoff = datetime.utcnow() - timedelta(hours=since_h)

    target_q = (args.get("target") or "").strip().lower()
    account_filter = (args.get("account_id") or "").strip()
    account_int: Optional[int] = None
    if account_filter.isdigit():
        account_int = int(account_filter)
    status_filter = (args.get("status") or "").strip()

    conv_rows = (
        db.query(SystemLog)
        .filter(
            SystemLog.component == CONV_COMPONENT,
            SystemLog.created_at >= cutoff,
        )
        .order_by(SystemLog.id.desc())
        .limit(500)
        .all()
    )

    duplicate_events: list[dict[str, Any]] = []
    for row in conv_rows:
        det = row.details if isinstance(row.details, dict) else {}
        cf = det.get("collected_fields") or {}
        if not isinstance(cf, dict):
            cf = {}
        tgt = (cf.get("target_username") or "").strip()
        aid = _safe_int(cf.get("account_id"))
        errs = det.get(LAST_PLAN_VALIDATION_ERRORS) or []
        if not isinstance(errs, list):
            errs = []
        if "duplicate_active_plan" not in errs:
            continue
        if account_int is not None and aid != account_int:
            continue
        fp_dup = _fingerprint_from_collected(cf)
        duplicate_events.append(
            {
                "time": row.created_at,
                "target": _trunc(tgt, 48),
                "account": str(aid) if aid is not None else "—",
                "fingerprint": fp_dup,
                "log_id": row.id,
                "errors": ", ".join(str(e) for e in errs[:6]),
            }
        )

    dup_fp_counts = Counter(d["fingerprint"] for d in duplicate_events)
    repeated_dupes = [
        {"fingerprint": fp, "attempts": n} for fp, n in dup_fp_counts.items() if n >= 2
    ]

    now = datetime.utcnow()
    expired_pending = 0
    orphan_drafts = 0
    stale_pending = 0
    for row in conv_rows:
        det = row.details if isinstance(row.details, dict) else {}
        cf = det.get("collected_fields") or {}
        if not isinstance(cf, dict):
            cf = {}
        exp = _parse_iso(det.get("expires_at"))
        created = _parse_iso(det.get("created_at"))
        if row.message == MSG_EXPIRED:
            expired_pending += 1
        if row.message == MSG_PENDING and exp and exp < now:
            orphan_drafts += 1
        if row.message == MSG_PENDING and created and (now - created) > timedelta(hours=24):
            stale_pending += 1

    conversations: list[dict[str, Any]] = []
    for row in conv_rows:
        det = row.details if isinstance(row.details, dict) else {}
        cf = det.get("collected_fields") or {}
        if not isinstance(cf, dict):
            cf = {}
        tgt = (cf.get("target_username") or "").strip()
        aid = _safe_int(cf.get("account_id"))
        owner = det.get("owner_telegram_id")
        intent = det.get("intent_type") or "—"
        exp = det.get("expires_at")

        if not _conv_status_match(row.message, status_filter):
            continue
        if account_int is not None and aid != account_int:
            continue
        if target_q and target_q not in tgt.lower():
            continue

        goal_snip = _trunc(str(cf.get("goal_text") or ""), 56)

        conversations.append(
            {
                "time": row.created_at,
                "owner": owner,
                "intent": _trunc(str(intent), 40),
                "target": _trunc(tgt, 40),
                "account": str(aid) if aid is not None else "—",
                "status": row.message,
                "status_badge": _conv_status_badge_class(row.message),
                "expires_at": exp or "—",
                "goal_snip": goal_snip,
                "log_id": row.id,
            }
        )

    plan_rows = (
        db.query(SystemLog)
        .filter(
            SystemLog.component == PLAN_COMPONENT,
            SystemLog.created_at >= cutoff,
        )
        .order_by(SystemLog.id.desc())
        .limit(400)
        .all()
    )
    plans_out: list[dict[str, Any]] = []
    for pr in plan_rows:
        pd = _plan_row_dict(pr)
        if account_int is not None:
            ac = pd["account"]
            if not (ac.isdigit() and int(ac) == account_int):
                continue
        if target_q and target_q not in (pd.get("target") or "").lower():
            continue
        if status_filter and status_filter.lower() not in (pd.get("status") or "").lower():
            continue
        plans_out.append(pd)

    ready_rows = (
        db.query(SystemLog)
        .filter(
            SystemLog.component == READY_COMPONENT,
            SystemLog.message == MSG_NOTIFIED,
            SystemLog.created_at >= cutoff,
        )
        .order_by(SystemLog.id.desc())
        .limit(200)
        .all()
    )
    ready_out: list[dict[str, Any]] = []
    for rr in ready_rows:
        d = rr.details or {}
        tid = d.get("task_id")
        owner = d.get("owner_telegram_id")
        ready_out.append(
            {
                "time": rr.created_at,
                "task_id": tid,
                "owner": owner,
                "send_outcome": "dedupe row (Telegram outcome not logged in v1)",
                "log_id": rr.id,
            }
        )

    return {
        "since_hours": since_h,
        "target_filter": target_q,
        "account_id_filter": account_filter,
        "status_filter": status_filter,
        "conversations": conversations[:250],
        "plans": plans_out[:250],
        "duplicate_events": duplicate_events[:120],
        "repeated_dupes": repeated_dupes,
        "ready_notifications": ready_out,
        "safety": {
            "expired_rows_in_window": expired_pending,
            "orphan_pending_expired_clock": orphan_drafts,
            "stale_pending_over_24h": stale_pending,
            "duplicate_attempt_rows": len(duplicate_events),
        },
        "constants": {
            "conv_statuses": [
                MSG_PENDING,
                MSG_COMPLETED,
                MSG_EXPIRED,
                CONV_MSG_CANCELLED,
                MSG_SUPERSEDED,
            ],
        },
    }


@dexpert_audit_bp.route("/dexpert")
@login_required
def dexpert_audit_page():
    """Read-only operator audit (conversations, plans, ready DMs, safety counters)."""
    with get_db_context() as db:
        ctx = _load_audit_context(db, request.args)
    return render_template("dexpert_audit.html", **ctx)


def register_dexpert_audit(app) -> None:
    """Register blueprint on the Flask app (called from ``register_routes``)."""
    app.register_blueprint(dexpert_audit_bp)
