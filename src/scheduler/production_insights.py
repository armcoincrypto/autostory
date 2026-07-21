"""
Read-only account reputation and target quality from ``message_deliveries`` + health.

No migrations — aggregates only. Intended for operator dashboards and guardrails.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func

from src.clients.readiness_store import (
    STAT_NOT_AUTH,
    STAT_TEMP,
    fetch_snapshot,
    snapshot_row_valid,
)
from src.clients.target_health import (
    HEALTH_INVALID,
    HEALTH_NEEDS_REPAIR,
    classify_target,
    merged_target_health_row,
)


def _utc_naive_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _norm_status(s: Any) -> str:
    if s is None:
        return ""
    v = s.value if hasattr(s, "value") else s
    return str(v).strip().upper()


def _classify_account_error_code(code: Optional[str]) -> str:
    c = (code or "").strip().upper()
    if c in ("FLOODWAIT", "SLOWMODEWAIT", "PEERFLOOD"):
        return "transient"
    if c in ("USERRESTRICTED",):
        return "restricted"
    if c in ("USERBANNEDINCHANNEL", "CHATWRITEFORBIDDEN", "CHATGUESTSENDFORBIDDEN", "CHATSENDPLAINFORBIDDEN"):
        return "hard_target"
    if c in ("CHANNELPRIVATE", "CHATRESTRICTED"):
        return "access"
    return "other"


def compute_account_reputation(db: Any, account_id: int, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """
    Rolling 24h window, naive UTC. ``risk_level``: healthy | watch | risky | blocked.
    """
    from src.core.scheduler_models import MessageDelivery

    now_naive = now or _utc_naive_now()
    since = now_naive - timedelta(hours=24)
    aid = int(account_id)
    _st = func.lower(func.trim(MessageDelivery.status))

    rows = (
        db.query(MessageDelivery.status, MessageDelivery.error_code, MessageDelivery.created_at)
        .filter(MessageDelivery.account_id == aid, MessageDelivery.created_at >= since)
        .order_by(MessageDelivery.created_at.desc())
        .limit(400)
        .all()
    )

    sent = failed = 0
    transient_fails = 0
    hard_fails = 0
    restricted_hits = 0
    last_success_at: Optional[datetime] = None
    last_failure_at: Optional[datetime] = None

    for st, ec, ca in rows:
        su = _norm_status(st)
        if su == "SENT":
            sent += 1
            if last_success_at is None:
                last_success_at = ca
        elif su == "FAILED":
            failed += 1
            if last_failure_at is None:
                last_failure_at = ca
            kind = _classify_account_error_code(ec)
            if kind == "transient":
                transient_fails += 1
            elif kind == "hard_target":
                hard_fails += 1
            elif kind == "restricted":
                restricted_hits += 1

    total = sent + failed
    failure_rate = (failed / total) if total else 0.0

    snap = fetch_snapshot(db, aid)
    risk = "healthy"
    reasons: List[str] = []

    if snap and snapshot_row_valid(snap, now_naive) and snap.status == STAT_NOT_AUTH:
        risk = "blocked"
        reasons.append("Readiness snapshot: NOT_AUTHORIZED")
    elif restricted_hits >= 1:
        risk = "blocked"
        reasons.append("USERRESTRICTED or similar in last 24h")
    elif failed >= 3 and hard_fails >= 2 and failure_rate >= 0.5:
        risk = "risky"
        reasons.append("Repeated hard target permission/ban errors")
    elif failure_rate >= 0.45 and failed >= 4:
        risk = "risky"
        reasons.append("High failure rate in last 24h")
    elif transient_fails >= 2 or failure_rate >= 0.25:
        risk = "watch"
        reasons.append("Transient rate-limit or elevated failures")
    elif snap and snapshot_row_valid(snap, now_naive) and snap.status == STAT_TEMP:
        risk = "watch"
        reasons.append("Readiness snapshot: TEMP_CONNECT")

    if risk == "healthy" and sent == 0 and failed > 0:
        risk = "watch"
        reasons.append("No successes yet with recent failures")

    return {
        "account_id": aid,
        "recent_sent_count_24h": sent,
        "recent_failed_count_24h": failed,
        "failure_rate_24h": round(failure_rate, 4),
        "last_success_at": last_success_at.isoformat() + "Z" if last_success_at else None,
        "last_failure_at": last_failure_at.isoformat() + "Z" if last_failure_at else None,
        "risk_level": risk,
        "reasons": reasons,
    }


def compute_target_quality(
    db: Any,
    target_id: int,
    *,
    account_id: Optional[int] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Rolling 7d delivery stats + health. ``risk_level``: good | watch | bad | needs_repair.
    """
    from src.core.scheduler_models import ChatTarget, MessageDelivery

    now_naive = now or _utc_naive_now()
    since = now_naive - timedelta(days=7)
    tid = int(target_id)

    t = db.query(ChatTarget).filter(ChatTarget.id == tid).first()
    intrinsic = classify_target(t) if t else {"health": HEALTH_INVALID, "reason": "missing"}
    mh = merged_target_health_row(db, t, account_id) if t else {"health": HEALTH_INVALID}
    merged_h = str(mh.get("health") or "")

    bad_health = {
        HEALTH_INVALID,
        HEALTH_NEEDS_REPAIR,
        "banned",
        "no_permission",
        "unresolved_entity",
    }
    if intrinsic.get("health") == HEALTH_NEEDS_REPAIR or intrinsic.get("health") == HEALTH_INVALID:
        return {
            "target_id": tid,
            "recent_sent_count_7d": 0,
            "recent_failed_count_7d": 0,
            "success_rate_7d": None,
            "last_success_at": None,
            "last_failure_at": None,
            "risk_level": "needs_repair",
            "health": merged_h,
            "reasons": [intrinsic.get("reason") or "target health"],
        }
    if merged_h in bad_health:
        return {
            "target_id": tid,
            "recent_sent_count_7d": 0,
            "recent_failed_count_7d": 0,
            "success_rate_7d": None,
            "last_success_at": None,
            "last_failure_at": None,
            "risk_level": "needs_repair" if merged_h in (HEALTH_NEEDS_REPAIR, HEALTH_INVALID, "unresolved_entity") else "bad",
            "health": merged_h,
            "reasons": [mh.get("health_reason") or merged_h],
        }

    _st = func.lower(func.trim(MessageDelivery.status))
    q = db.query(MessageDelivery).filter(
        MessageDelivery.target_id == tid,
        MessageDelivery.created_at >= since,
    )
    if account_id is not None:
        q = q.filter(MessageDelivery.account_id == int(account_id))

    rows = q.order_by(MessageDelivery.created_at.desc()).limit(300).all()
    sent = failed = 0
    transient_fails = 0
    perm_fails = 0
    last_success_at = None
    last_failure_at = None
    for d in rows:
        su = _norm_status(d.status)
        if su == "SENT":
            sent += 1
            if last_success_at is None:
                last_success_at = d.created_at
        elif su == "FAILED":
            failed += 1
            if last_failure_at is None:
                last_failure_at = d.created_at
            k = _classify_account_error_code(getattr(d, "error_code", None))
            if k == "transient":
                transient_fails += 1
            elif k in ("hard_target", "access"):
                perm_fails += 1

    tot = sent + failed
    success_rate = (sent / tot) if tot else None
    risk = "good"
    reasons: List[str] = []

    if tot == 0:
        risk = "watch"
        reasons.append("No deliveries in last 7d")
    elif perm_fails >= 3 or (failed >= 4 and (success_rate is not None and success_rate < 0.35)):
        risk = "bad"
        reasons.append("Repeated permission or access failures")
    elif transient_fails >= 2 or (failed >= 2 and (success_rate is not None and success_rate < 0.55)):
        risk = "watch"
        reasons.append("Transient or mixed failure pattern")

    return {
        "target_id": tid,
        "recent_sent_count_7d": sent,
        "recent_failed_count_7d": failed,
        "success_rate_7d": round(success_rate, 4) if success_rate is not None else None,
        "last_success_at": last_success_at.isoformat() + "Z" if last_success_at else None,
        "last_failure_at": last_failure_at.isoformat() + "Z" if last_failure_at else None,
        "risk_level": risk,
        "health": merged_h,
        "reasons": reasons,
    }


def batch_target_quality(
    db: Any,
    target_ids: List[int],
    *,
    account_id: Optional[int] = None,
    now: Optional[datetime] = None,
) -> Dict[int, Dict[str, Any]]:
    out: Dict[int, Dict[str, Any]] = {}
    for tid in target_ids:
        try:
            out[int(tid)] = compute_target_quality(db, int(tid), account_id=account_id, now=now)
        except Exception:
            out[int(tid)] = {
                "target_id": int(tid),
                "risk_level": "watch",
                "reasons": ["quality computation error"],
            }
    return out


def all_accounts_reputation(db: Any, *, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    from src.core.models import Account

    accounts = db.query(Account).order_by(Account.id).all()
    return [compute_account_reputation(db, int(a.id), now=now) for a in accounts]


def count_risky_accounts(db: Any, *, now: Optional[datetime] = None) -> Dict[str, int]:
    rows = all_accounts_reputation(db, now=now)
    out = {"healthy": 0, "watch": 0, "risky": 0, "blocked": 0}
    for r in rows:
        lvl = str(r.get("risk_level") or "healthy")
        if lvl in out:
            out[lvl] += 1
        else:
            out["watch"] += 1
    return out


def count_bad_targets(db: Any, *, now: Optional[datetime] = None) -> int:
    from src.core.scheduler_models import ChatTarget

    n = 0
    for t in db.query(ChatTarget).all():
        q = compute_target_quality(db, int(t.id), account_id=None, now=now)
        if str(q.get("risk_level")) in ("bad", "needs_repair"):
            n += 1
    return n
