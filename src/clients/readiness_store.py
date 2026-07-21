"""
V1 ``account_readiness_snapshots`` helpers (read + write).

Used by the dedicated readiness worker, scheduler deep checks, and dashboard overlays.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import structlog
from sqlalchemy.orm import Session

from src.readiness.readiness_store_v2 import read_snapshot as _read_v1_view

logger = structlog.get_logger(__name__)

# Status constants (legacy v1 strings)
_TRANSIENT_FAILURE_CODES = frozenset({
    "failed_connect",
    "session_db_locked",
    "session_lock_timeout",
    "failed_connect_network",
})
_SESSION_LOCK_PRESERVE_CODES = frozenset({
    "session_lock_timeout",
    "session_db_locked",
})

# Status constants (legacy v1 strings)
STAT_READY = "READY"
STAT_ERROR = "ERROR"
STAT_NOT_AUTH = "NOT_AUTHORIZED"
STAT_TEMP = "TEMP_CONNECT"
STAT_VERIFY = "VERIFY"

READY_TTL_SEC = int(os.environ.get("READINESS_READY_TTL_SEC", "3600"))
TEMP_TTL_SEC = int(os.environ.get("READINESS_TEMP_TTL_SEC", "600"))
ERROR_TTL_SEC = int(os.environ.get("READINESS_ERROR_TTL_SEC", "1800"))
SESSION_MATERIAL_ERROR_TTL_SEC = int(os.environ.get("READINESS_SESSION_MATERIAL_ERROR_TTL_SEC", "3600"))
READINESS_TRUST_WINDOW_SEC = int(os.environ.get("READINESS_TRUST_WINDOW_SEC", "900"))


def _now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _as_utc_aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass
class ReadinessSnapshot:
    """Legacy-shaped snapshot row for production_insights / scheduler overlays."""

    account_id: int
    status: str
    failure_code: Optional[str]
    reason: Optional[str]
    checked_at: Optional[datetime]
    expires_at: Optional[datetime]


def _upsert_snapshot(
    db: Session,
    account_id: int,
    *,
    status: str,
    reason: Optional[str],
    failure_code: Optional[str],
    checked_at: datetime,
    expires_at: Optional[datetime],
) -> None:
    from src.core.scheduler_models import AccountReadinessSnapshot

    aid = int(account_id)
    row = (
        db.query(AccountReadinessSnapshot)
        .filter(AccountReadinessSnapshot.account_id == aid)
        .first()
    )
    reason_trim = (reason or "")[:4000] if reason else None
    fc_trim = (failure_code or "")[:64] if failure_code else None
    if row:
        row.status = status
        row.reason = reason_trim
        row.failure_code = fc_trim
        row.checked_at = checked_at
        row.expires_at = expires_at
        return
    db.add(
        AccountReadinessSnapshot(
            account_id=aid,
            status=status,
            reason=reason_trim,
            failure_code=fc_trim,
            checked_at=checked_at,
            expires_at=expires_at,
        )
    )


def fetch_snapshot(db: Session, account_id: int) -> Optional[ReadinessSnapshot]:
    """Read v1 snapshot only (conservative)."""
    view = _read_v1_view(db, int(account_id))
    if view.status.value in ("NOT_FOUND", "TABLE_MISSING", "UNKNOWN"):
        return None
    checked = None
    expires = None
    if view.checked_at:
        try:
            s = str(view.checked_at).replace("Z", "+00:00")
            checked = datetime.fromisoformat(s).replace(tzinfo=None)
        except Exception:
            checked = None
    if view.expires_at:
        try:
            s = str(view.expires_at).replace("Z", "+00:00")
            expires = datetime.fromisoformat(s).replace(tzinfo=None)
        except Exception:
            expires = None
    st = view.status.value
    if st == "TEMP_CONNECT":
        st = STAT_TEMP
    elif st == "NOT_AUTHORIZED":
        st = STAT_NOT_AUTH
    return ReadinessSnapshot(
        account_id=int(account_id),
        status=st,
        failure_code=view.failure_code,
        reason=view.reason,
        checked_at=checked,
        expires_at=expires,
    )


def snapshot_is_expired(snap: ReadinessSnapshot, now: Optional[datetime] = None) -> bool:
    now_naive = now or _now_naive()
    if snap.expires_at is not None:
        return now_naive >= snap.expires_at
    if snap.checked_at is None:
        return True
    ttl = READY_TTL_SEC
    if snap.status == STAT_TEMP:
        ttl = TEMP_TTL_SEC
    elif snap.status in (STAT_ERROR, STAT_NOT_AUTH):
        ttl = ERROR_TTL_SEC
    return (now_naive - snap.checked_at).total_seconds() > ttl


def snapshot_row_valid(snap: Optional[ReadinessSnapshot], now: Optional[datetime] = None) -> bool:
    if snap is None:
        return False
    return not snapshot_is_expired(snap, now)


def snapshot_ready_trusted(snap: Optional[ReadinessSnapshot], now: Optional[datetime] = None) -> bool:
    if not snapshot_row_valid(snap, now):
        return False
    if snap.status != STAT_READY:
        return False
    now_naive = now or _now_naive()
    if snap.checked_at is None:
        return False
    age = (now_naive - snap.checked_at).total_seconds()
    return age <= READINESS_TRUST_WINDOW_SEC


def snapshot_ready_and_valid(db: Session, account_id: int) -> bool:
    snap = fetch_snapshot(db, int(account_id))
    return snapshot_ready_trusted(snap)


def readiness_state_from_snapshot(snap: Optional[ReadinessSnapshot], now: Optional[datetime] = None) -> str:
    if snap is None or not snapshot_row_valid(snap, now):
        return "UNKNOWN"
    if snap.status == STAT_READY:
        if snapshot_ready_trusted(snap, now):
            return "READY"
        return "STALE"
    if snap.status == STAT_TEMP:
        return "RETRY"
    if snap.status == STAT_NOT_AUTH:
        return "NOT_AUTHORIZED"
    if snap.status == STAT_ERROR:
        return "ERROR"
    return str(snap.status or "UNKNOWN").upper()


def readiness_failure_kind(snap: Optional[ReadinessSnapshot]) -> Optional[str]:
    if snap is None:
        return None
    if snap.status == STAT_NOT_AUTH:
        return "auth"
    if snap.status == STAT_TEMP:
        return "transient"
    if snap.status == STAT_ERROR:
        return "error"
    return None


def overlay_cached_readiness(row: Dict[str, Any], snap: Optional[ReadinessSnapshot], *, now: Optional[datetime] = None) -> None:
    now_naive = now or _now_naive()
    if snap is None:
        row.setdefault("readiness_status", "UNKNOWN")
        row.setdefault("readiness_state", "UNKNOWN")
        return
    valid = snapshot_row_valid(snap, now_naive)
    row["readiness_status"] = snap.status if valid else "UNKNOWN"
    row["readiness_state"] = readiness_state_from_snapshot(snap if valid else None, now_naive)
    row["readiness_failure_kind"] = readiness_failure_kind(snap) if valid else None
    if snap.checked_at:
        row["readiness_checked_at"] = snap.checked_at.isoformat() + "Z"
    if snap.expires_at:
        row["readiness_expires_at"] = snap.expires_at.isoformat() + "Z"
    if snap.failure_code:
        row["readiness_failure_code"] = snap.failure_code
    if snap.reason:
        row["readiness_reason"] = snap.reason


def overlay_expired_readiness(row: Dict[str, Any], snap: Optional[ReadinessSnapshot]) -> None:
    row["readiness_status"] = "UNKNOWN"
    row["readiness_state"] = "STALE" if snap and snap.status == STAT_READY else "UNKNOWN"
    row["readiness_failure_kind"] = None


def apply_cached_readiness_to_rows(db: Session, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge v1 DB snapshots into shallow manager rows (read-only)."""
    now = _now_naive()
    for row in rows:
        if not row or row.get("account_id") is None:
            continue
        snap = fetch_snapshot(db, int(row["account_id"]))
        if snap and snapshot_row_valid(snap, now):
            overlay_cached_readiness(row, snap, now=now)
        elif snap:
            overlay_expired_readiness(row, snap)
        else:
            overlay_cached_readiness(row, None, now=now)
    return rows


def compute_campaign_ready_fields(
    *,
    account_status: Optional[str] = None,
    purpose: Optional[str] = None,
    readiness_status: Optional[str] = None,
    readiness_state: Optional[str] = None,
    readiness_failure_kind: Optional[str] = None,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    """Pure helper for scheduler UI (conservative)."""
    purpose_l = (purpose or "both").strip().lower()
    acc_st = (account_status or "").strip().lower()
    r_st = (readiness_status or "").strip().upper()
    r_state = (readiness_state or "").strip().upper()

    campaign_relevant = purpose_l in ("both", "autostory", "messaging", "")
    if purpose_l == "ai_agent":
        return {
            "campaign_relevant": False,
            "campaign_ready": False,
            "campaign_ready_label": "AI Agent (not campaign)",
            "campaign_ready_reason": "purpose=ai_agent",
            "campaign_readiness_status": r_st or None,
            "campaign_readiness_state": r_state or None,
        }

    if acc_st and acc_st != "active":
        return {
            "campaign_relevant": campaign_relevant,
            "campaign_ready": False,
            "campaign_ready_label": "Account inactive",
            "campaign_ready_reason": f"account_status={acc_st}",
            "campaign_readiness_status": r_st or None,
            "campaign_readiness_state": r_state or None,
        }

    if error:
        return {
            "campaign_relevant": campaign_relevant,
            "campaign_ready": False,
            "campaign_ready_label": "Error",
            "campaign_ready_reason": str(error)[:200],
            "campaign_readiness_status": r_st or None,
            "campaign_readiness_state": r_state or None,
        }

    campaign_ready = r_st == STAT_READY and r_state == "READY"

    label = "Not ready"
    reason = readiness_failure_kind or r_state or r_st or "unknown"
    if campaign_ready:
        label = "Campaign Ready"
        reason = "Readiness snapshot READY (authorized)."
    elif r_state == "STALE":
        label = "Ready (stale)"
        reason = "Readiness snapshot is READY but older than trust window; recheck recommended."
    elif r_st == STAT_NOT_AUTH:
        label = "Re-login required"
        reason = "NOT_AUTHORIZED"
    elif r_st == STAT_TEMP or r_state == "RETRY":
        label = "Retry"
        reason = "TEMP_CONNECT"

    return {
        "campaign_relevant": campaign_relevant,
        "campaign_ready": campaign_ready,
        "campaign_ready_label": label,
        "campaign_ready_reason": reason,
        "campaign_readiness_status": r_st or None,
        "campaign_readiness_state": r_state or None,
    }


def detect_duplicate_targets(db: Session) -> Dict[str, Any]:
    """Read-only duplicate chat_targets report."""
    from collections import defaultdict

    from src.core.scheduler_models import ChatTarget

    by_user: dict[str, list[int]] = defaultdict(list)
    by_tg: dict[str, list[int]] = defaultdict(list)
    for t in db.query(ChatTarget).all():
        tid = int(t.id)
        un = (getattr(t, "username", None) or "").strip().lstrip("@").lower()
        if un:
            by_user[un].append(tid)
        tg = getattr(t, "tg_id", None)
        if tg is not None:
            by_tg[str(tg)].append(tid)

    groups: list[dict[str, Any]] = []
    for key, ids in by_user.items():
        if len(ids) > 1:
            groups.append({"group_key": f"username:{key}", "target_ids": sorted(ids)})
    for key, ids in by_tg.items():
        if len(ids) > 1:
            groups.append({"group_key": f"tg_id:{key}", "target_ids": sorted(ids)})

    return {
        "duplicate_groups": groups,
        "group_count": len(groups),
        "read_only": True,
        "shim": "p9_11",
    }


def persist_from_manager_deep_row(db: Session, row: Dict[str, Any]) -> None:
    """Persist result of a live Telethon deep readiness check."""
    aid = int(row.get("account_id") or 0)
    if not aid:
        return
    now = _now_naive()
    ready = bool(row.get("ready"))
    auth = row.get("authorized")
    fk = row.get("readiness_failure_kind")
    fc = row.get("failure_code")
    err = row.get("error")
    err_s = str(err) if err is not None else ""

    if ready and auth is True:
        _upsert_snapshot(
            db,
            aid,
            status=STAT_READY,
            reason=None,
            failure_code=None,
            checked_at=now,
            expires_at=now + timedelta(seconds=READY_TTL_SEC),
        )
        return

    if auth is False:
        _upsert_snapshot(
            db,
            aid,
            status=STAT_NOT_AUTH,
            reason=err_s or "Session not authorized with Telegram",
            failure_code=str(fc or "unauthorized_session"),
            checked_at=now,
            expires_at=None,
        )
        return

    if fk == "transient" or (fc and str(fc) in _TRANSIENT_FAILURE_CODES):
        if should_preserve_ready_on_session_lock_contention(db, aid, str(fc) if fc else None):
            logger.info("readiness_preserve_ready_on_lock", account_id=aid, failure_code=fc)
            return
        _upsert_snapshot(
            db,
            aid,
            status=STAT_TEMP,
            reason=err_s or human_msg_temp(str(fc) if fc else None),
            failure_code=str(fc) if fc else None,
            checked_at=now,
            expires_at=now + timedelta(seconds=TEMP_TTL_SEC),
        )
        return

    if err_s and not ready:
        _upsert_snapshot(
            db,
            aid,
            status=STAT_ERROR,
            reason=err_s[:2000],
            failure_code=str(fc) if fc else None,
            checked_at=now,
            expires_at=now + timedelta(seconds=ERROR_TTL_SEC),
        )


def mark_account_ready_after_success(db: Session, account_id: int, reason: str) -> None:
    now = _now_naive()
    _upsert_snapshot(
        db,
        int(account_id),
        status=STAT_READY,
        reason=(reason or "")[:500],
        failure_code=None,
        checked_at=now,
        expires_at=now + timedelta(seconds=READY_TTL_SEC),
    )
    logger.debug("readiness_mark_ready", account_id=int(account_id), reason=reason)


def mark_account_not_authorized(db: Session, account_id: int, reason: str) -> None:
    now = _now_naive()
    _upsert_snapshot(
        db,
        int(account_id),
        status=STAT_NOT_AUTH,
        reason=(reason or "")[:2000],
        failure_code="unauthorized_session",
        checked_at=now,
        expires_at=None,
    )


def mark_account_temp_connect(
    db: Session,
    account_id: int,
    reason: str,
    failure_code: Optional[str] = None,
) -> None:
    now = _now_naive()
    _upsert_snapshot(
        db,
        int(account_id),
        status=STAT_TEMP,
        reason=(reason or "")[:2000],
        failure_code=(failure_code or "")[:64] if failure_code else None,
        checked_at=now,
        expires_at=now + timedelta(seconds=TEMP_TTL_SEC),
    )


def mark_account_readiness_error(
    db: Session,
    account_id: int,
    reason: str,
    *,
    failure_code: Optional[str] = None,
) -> None:
    now = _now_naive()
    _upsert_snapshot(
        db,
        int(account_id),
        status=STAT_ERROR,
        reason=(reason or "")[:2000],
        failure_code=(failure_code or "")[:64] if failure_code else None,
        checked_at=now,
        expires_at=now + timedelta(seconds=SESSION_MATERIAL_ERROR_TTL_SEC),
    )
    logger.info(
        "readiness_mark_error",
        account_id=int(account_id),
        failure_code=failure_code,
    )


def should_preserve_ready_on_session_lock_contention(
    db: Session,
    account_id: int,
    failure_code: Optional[str] = None,
) -> bool:
    fc = (failure_code or "").strip()
    if fc not in _SESSION_LOCK_PRESERVE_CODES:
        return False
    snap = fetch_snapshot(db, int(account_id))
    return bool(snap and getattr(snap, "status", None) == STAT_READY)


def human_msg_temp(failure_code: Optional[str]) -> str:
    from src.clients.session_resolve import human_message_for_code

    if failure_code:
        msg = human_message_for_code(str(failure_code))
        if msg:
            return msg
        return str(failure_code)
    return "Temporary connect issue"
