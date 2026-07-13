"""
P6 — Operator control read models (read-only aggregation for dashboard pages).

Reuses canonical readiness, eligibility, and scheduler models. No execution side effects.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import structlog

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.clients import readiness_store
from src.clients.readiness_worker_policy import read_runtime_status
from src.core.account_operational_state import compute_account_operational_state
from src.core.datetime_utc import to_utc_iso_z
from src.core.models import Account
from src.core.scheduler_models import (
    AccountReadinessSnapshot,
    AccountTargetBinding,
    AccountTargetMembershipProbe,
    ChatTarget,
    JobStatus,
    MessageDelivery,
    ScheduleProfile,
    ScheduleRule,
    ScheduledJob,
)
from src.core.p5d_gateway_reconciliation import (
    AMBIGUOUS_RECONCILIATION_REQUIRED,
    NOT_SENT_CONFIRMED,
    SENT_CONFIRMED,
    TERMINAL_POLICY_DENIAL,
    _lookup_message_by_body,
)
from src.scheduler.generation_eligibility import (
    evaluate_generation_eligibility,
    production_certified_no_go_active,
    promo_generation_mode,
)
from src.telegram_gateway.models import TelegramGatewayJob

logger = structlog.get_logger(__name__)

PROTECTED_DELIVERY_IDS = frozenset({143, 144, 145, 148, 149, 150})
PROTECTED_JOB_IDS = frozenset({329, 361, 362, 363, 364, 365, 366})


def _git_short_head() -> str:
    root = Path(os.environ.get("AUTOSTORY_ROOT", "/opt/autostory"))
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=str(root),
                text=True,
                timeout=3,
            )
            .strip()
        )
    except Exception:
        pass
    try:
        head_file = root / ".git" / "HEAD"
        ref = head_file.read_text(encoding="utf-8").strip()
        if ref.startswith("ref: "):
            ref_path = root / ".git" / ref[5:].strip()
            return ref_path.read_text(encoding="utf-8").strip()[:12]
        return ref[:12]
    except Exception:
        return "unknown"


def build_info_snapshot() -> dict[str, Any]:
    """Safe build marker for production artifact verification."""
    return {
        "git_commit": _git_short_head(),
        "production_no_go": production_certified_no_go_active(),
        "readiness_worker_enabled": (
            os.environ.get("READINESS_WORKER_ENABLED", "true").strip().lower()
            in ("1", "true", "yes", "on")
        ),
        "readiness_worker_note": (
            "Web gunicorn sets READINESS_WORKER_ENABLED=false; use dedicated "
            "autostory-readiness-worker or inline refresh on account detail."
        ),
    }


def same_day_duplicate_status(
    db: Session,
    *,
    account_id: int,
    target_id: int,
    job_type: str,
    timezone_name: Optional[str],
) -> dict[str, Any]:
    """Read-only same-day job presence (mirrors generator._job_exists semantics)."""
    try:
        tz = ZoneInfo((timezone_name or "UTC").strip())
    except Exception:
        tz = ZoneInfo("UTC")
    local_day = datetime.now(tz).date()
    start_local = datetime(local_day.year, local_day.month, local_day.day, 0, 0, 0, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    day_start_utc = start_local.astimezone(timezone.utc).replace(tzinfo=None)
    day_end_utc = end_local.astimezone(timezone.utc).replace(tzinfo=None)
    active_statuses = (
        JobStatus.PENDING.value,
        JobStatus.SKIPPED.value,
        JobStatus.SENT.value,
        JobStatus.RUNNING.value,
    )
    jobs = (
        db.query(ScheduledJob)
        .filter(
            ScheduledJob.account_id == int(account_id),
            ScheduledJob.target_id == int(target_id),
            ScheduledJob.type == job_type,
            ScheduledJob.run_at >= day_start_utc,
            ScheduledJob.run_at < day_end_utc,
            ScheduledJob.status.in_(active_statuses),
        )
        .order_by(ScheduledJob.id.asc())
        .all()
    )
    return {
        "local_date": str(local_day),
        "exists": bool(jobs),
        "would_block_generation": bool(jobs),
        "jobs": [
            {
                "id": int(j.id),
                "status": _status_str(j.status),
                "run_at": _iso(j.run_at),
            }
            for j in jobs
        ],
    }


def compute_next_occurrence(rule: ScheduleRule, profile: Optional[ScheduleProfile]) -> dict[str, Any]:
    """Next fixed HH:MM occurrence in profile timezone; explicit unavailable for RANDOM rules."""
    tz_name = (profile.timezone if profile else None) or "UTC"
    try:
        tz = ZoneInfo(tz_name.strip())
    except Exception:
        return {"available": False, "reason": f"invalid_timezone:{tz_name}"}
    try:
        times = json.loads(rule.times_json) if isinstance(rule.times_json, str) else rule.times_json
    except (json.JSONDecodeError, TypeError):
        times = []
    if not times:
        return {"available": False, "reason": "no_schedule_times"}
    now_local = datetime.now(tz)
    candidates: list[datetime] = []
    for time_str in times:
        if not isinstance(time_str, str):
            continue
        if time_str.strip().upper().startswith("RANDOM"):
            return {
                "available": False,
                "reason": "random_window_requires_scheduler",
                "detail": "RANDOM windows are resolved at generation time only.",
            }
        try:
            h, m = map(int, time_str.split(":"))
        except (ValueError, IndexError):
            continue
        for offset in (0, 1):
            d = now_local.date() + timedelta(days=offset)
            dt = datetime(d.year, d.month, d.day, h, m, tzinfo=tz)
            if dt > now_local:
                candidates.append(dt)
    if not candidates:
        return {"available": False, "reason": "no_future_fixed_times_today"}
    nxt = min(candidates)
    utc_naive = nxt.astimezone(timezone.utc).replace(tzinfo=None)
    return {
        "available": True,
        "next_at_utc": _iso(utc_naive),
        "next_at_local": nxt.isoformat(),
        "timezone": tz_name,
    }


def _find_gateway_for_scheduled_job(db: Session, scheduled_job_id: int) -> Optional[TelegramGatewayJob]:
    rows = (
        db.query(TelegramGatewayJob)
        .order_by(TelegramGatewayJob.id.desc())
        .limit(200)
        .all()
    )
    for row in rows:
        payload = row.payload_json if isinstance(row.payload_json, dict) else {}
        if payload.get("scheduled_job_id") == int(scheduled_job_id):
            return row
    return None


async def run_delivery_reconciliation_check(db: Session, delivery_id: int) -> dict[str, Any]:
    """
    Non-sending reconciliation inspect using P5D Telegram lookup helpers.

    Does not send messages, reset jobs, or consume counters.
    """
    delivery = db.get(MessageDelivery, int(delivery_id)) if hasattr(db, "get") else (
        db.query(MessageDelivery).filter(MessageDelivery.id == int(delivery_id)).first()
    )
    if delivery is None:
        return {"ok": False, "error": "delivery_not_found"}

    audit: dict[str, Any] = {
        "delivery_id": int(delivery.id),
        "job_id": delivery.job_id,
        "account_id": delivery.account_id,
        "target_id": delivery.target_id,
        "protected_record": int(delivery.id) in PROTECTED_DELIVERY_IDS,
    }
    st = _status_str(delivery.status).upper()
    err = (delivery.error_code or "").upper()

    if st == "SENT" and delivery.tg_message_id is not None:
        return {
            "ok": True,
            "outcome": SENT_CONFIRMED,
            "reason_code": "delivery_sent_with_message_id",
            "tg_message_id": int(delivery.tg_message_id),
            "audit": audit,
            "mutated": False,
        }

    if err in ("POLICY", "DENIED", "NOT_ALLOWED") or "POLICY" in err:
        return {
            "ok": True,
            "outcome": TERMINAL_POLICY_DENIAL,
            "reason_code": delivery.error_code or "policy_denial",
            "audit": audit,
            "mutated": False,
        }

    gateway = None
    if delivery.job_id:
        gateway = _find_gateway_for_scheduled_job(db, int(delivery.job_id))
    if gateway is not None:
        audit["gateway_job_id"] = int(gateway.id)
        audit["gateway_status"] = gateway.status
        res = gateway.result_json if isinstance(gateway.result_json, dict) else {}
        tid = res.get("telegram_message_id")
        if str(gateway.status or "").lower() == "done" and tid is not None:
            return {
                "ok": True,
                "outcome": SENT_CONFIRMED,
                "reason_code": "gateway_done_with_message_id",
                "tg_message_id": int(tid),
                "audit": audit,
                "mutated": False,
            }

    target = db.get(ChatTarget, int(delivery.target_id)) if hasattr(db, "get") else (
        db.query(ChatTarget).filter(ChatTarget.id == int(delivery.target_id)).first()
    )
    target_ref = ""
    if target is not None:
        target_ref = (
            (target.username or "").strip()
            or (getattr(target, "invite_link", None) or "")
            or (str(getattr(target, "tg_id", "") or ""))
        )
    body = (delivery.rendered_body or "").strip()
    if body and target_ref:
        looked_up = await _lookup_message_by_body(int(delivery.account_id), target_ref, body)
        if looked_up is not None:
            audit["telegram_lookup_tg_message_id"] = looked_up
            return {
                "ok": True,
                "outcome": SENT_CONFIRMED,
                "reason_code": "telegram_lookup_confirmed_sent",
                "tg_message_id": int(looked_up),
                "audit": audit,
                "mutated": False,
            }

    if st in ("FAILED", "ERROR", "SENDING") and not delivery.tg_message_id:
        return {
            "ok": True,
            "outcome": AMBIGUOUS_RECONCILIATION_REQUIRED,
            "reason_code": "no_durable_send_proof",
            "audit": audit,
            "mutated": False,
        }

    return {
        "ok": True,
        "outcome": NOT_SENT_CONFIRMED,
        "reason_code": "no_send_evidence",
        "audit": audit,
        "mutated": False,
    }


async def refresh_binding_permission(db: Session, binding_id: int, *, force_refresh: bool = True) -> dict[str, Any]:
    """Canonical membership probe for one binding; updates probe cache only."""
    binding = db.get(AccountTargetBinding, int(binding_id)) if hasattr(db, "get") else (
        db.query(AccountTargetBinding).filter(AccountTargetBinding.id == int(binding_id)).first()
    )
    if binding is None:
        return {"ok": False, "error": "binding_not_found"}

    from src.clients.membership_check import check_targets_membership_sequential, upsert_membership_probe
    from src.clients.manager import client_manager as _client_manager
    from src.core.models import Account

    account_id = int(binding.account_id)
    target_id = int(binding.target_id)
    before = queue_counts_snapshot(db)

    acc = db.query(Account).filter(Account.id == account_id).first()
    if acc is None:
        return {"ok": False, "error": "account_not_found"}

    pooled = False
    try:
        wrapper, gate_err = await _client_manager.add_account(acc)
        if wrapper is not None:
            pooled = True
        elif gate_err:
            return {"ok": False, "error": gate_err, "before": before, "after": before}

        results = await check_targets_membership_sequential(account_id, [target_id])
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for row in results:
            upsert_membership_probe(db, account_id, row, checked_at=now)
        db.commit()
    finally:
        if pooled:
            try:
                await _client_manager.remove_account(account_id)
            except Exception as exc:
                logger.warning("binding_refresh_remove_account_failed", account_id=account_id, error=str(exc))

    after = queue_counts_snapshot(db)
    probe = (
        db.query(AccountTargetMembershipProbe)
        .filter(
            AccountTargetMembershipProbe.account_id == account_id,
            AccountTargetMembershipProbe.target_id == target_id,
        )
        .order_by(AccountTargetMembershipProbe.checked_at.desc())
        .first()
    )
    return {
        "ok": True,
        "binding_id": int(binding_id),
        "account_id": account_id,
        "target_id": target_id,
        "membership_status": probe.status if probe else None,
        "can_post": bool(probe.can_post) if probe and probe.can_post is not None else bool(binding.can_post),
        "permission_checked_at": _iso(probe.checked_at) if probe else None,
        "message": (probe.message or "")[:300] if probe else None,
        "non_mutating_proof": {"before": before, "after": after, "execution_unchanged": before == after},
    }


_DANGEROUS_ENV_KEYS = (
    "AUTOSTORY_PRODUCTION_CERTIFIED_NO_GO",
    "PROMO_GENERATION_MODE",
    "SCHEDULER_MUTATIONS_ENABLED",
    "P5C_SINGLE_SEND_ENABLED",
    "P5D_SINGLE_SEND_ENABLED",
    "P5D_CERTIFICATION_MODE",
    "P5D_FAILPOINT",
)


def _iso(dt: Any) -> Optional[str]:
    if dt is None:
        return None
    return to_utc_iso_z(dt)


def _svc_active(name: str) -> str:
    try:
        out = subprocess.run(
            ["systemctl", "is-active", name],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return (out.stdout or "").strip() or "unknown"
    except Exception:
        return "unknown"


def _svc_enabled(name: str) -> str:
    try:
        out = subprocess.run(
            ["systemctl", "is-enabled", name],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return (out.stdout or "").strip() or "unknown"
    except Exception:
        return "unknown"


def _readiness_freshness(db: Session, account_id: int) -> dict[str, Any]:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    snap = readiness_store.fetch_snapshot(db, int(account_id))
    if snap is None:
        return {
            "status": None,
            "reason": None,
            "failure_code": None,
            "checked_at": None,
            "expires_at": None,
            "freshness": "missing",
            "display_status": "PROBE_REQUIRED",
        }
    valid = readiness_store.snapshot_row_valid(snap, now)
    fresh_ready = readiness_store.snapshot_ready_and_valid(db, int(account_id))
    status = (snap.status or "").strip().upper()
    if status == readiness_store.STAT_READY and fresh_ready:
        freshness = "fresh"
        display = "READY"
    elif status == readiness_store.STAT_READY:
        freshness = "stale"
        display = "STALE_READY"
    elif status == readiness_store.STAT_NOT_AUTH:
        freshness = "blocked"
        display = "NOT_AUTHORIZED"
    elif not valid:
        freshness = "expired"
        display = "PROBE_REQUIRED"
    elif status == readiness_store.STAT_ERROR:
        freshness = "failed"
        display = "FAILED_AUTH"
    elif status == readiness_store.STAT_TEMP:
        freshness = "transient"
        display = "PROBE_REQUIRED"
    else:
        freshness = "unknown"
        display = status or "UNKNOWN"
    return {
        "status": status,
        "reason": snap.reason,
        "failure_code": snap.failure_code,
        "checked_at": _iso(snap.checked_at),
        "expires_at": _iso(snap.expires_at),
        "freshness": freshness,
        "display_status": display,
    }


def _account_label(account: Account) -> str:
    username = (account.username or "").strip()
    if username:
        return username if username.startswith("@") else f"@{username}"
    phone = (account.phone_number or "").strip()
    if phone:
        return phone[:4] + "…" + phone[-2:] if len(phone) > 6 else phone
    return f"account_{int(account.id)}"


def _status_str(val: Any) -> str:
    if val is None:
        return ""
    if hasattr(val, "value"):
        return str(val.value)
    return str(val)


def build_account_inventory_row(db: Session, account: Account) -> dict[str, Any]:
    aid = int(account.id)
    op = compute_account_operational_state(db, account)
    readiness = _readiness_freshness(db, aid)
    status_val = _status_str(getattr(account, "status", "")).lower()
    enabled = status_val == "active"
    tier = (op.get("tier") or "").lower()
    quarantined = tier in ("reserved", "controller") or bool(op.get("quarantined"))

    bound_targets = (
        db.query(func.count(AccountTargetBinding.id))
        .filter(AccountTargetBinding.account_id == aid)
        .scalar()
        or 0
    )
    profile = db.query(ScheduleProfile).filter(ScheduleProfile.account_id == aid).first()
    active_schedules = 0
    if profile and profile.is_enabled:
        active_schedules = (
            db.query(func.count(ScheduleRule.id))
            .filter(ScheduleRule.account_id == aid, ScheduleRule.is_enabled.is_(True))
            .scalar()
            or 0
        )
    pending_running = (
        db.query(func.count(ScheduledJob.id))
        .filter(
            ScheduledJob.account_id == aid,
            ScheduledJob.status.in_(("PENDING", "RUNNING")),
        )
        .scalar()
        or 0
    )
    last_delivery = (
        db.query(MessageDelivery)
        .filter(MessageDelivery.account_id == aid)
        .order_by(MessageDelivery.created_at.desc())
        .first()
    )
    blocked_reasons: list[str] = []
    if not enabled:
        blocked_reasons.append("account_disabled")
    if quarantined:
        blocked_reasons.append(f"quarantined_tier:{tier}")
    if readiness["display_status"] in ("NOT_AUTHORIZED", "FAILED_AUTH"):
        blocked_reasons.append(readiness["display_status"].lower())
    if readiness["display_status"] == "STALE_READY":
        blocked_reasons.append("readiness_stale")

    return {
        "id": aid,
        "label": _account_label(account),
        "enabled": enabled,
        "authorized": readiness["display_status"] not in ("NOT_AUTHORIZED", "FAILED_AUTH"),
        "readiness_status": readiness["display_status"],
        "readiness_reason": readiness.get("reason") or readiness.get("failure_code"),
        "readiness_freshness": readiness["freshness"],
        "checked_at": readiness["checked_at"],
        "expires_at": readiness["expires_at"],
        "quarantined": quarantined,
        "bound_target_count": int(bound_targets),
        "active_schedule_count": int(active_schedules),
        "pending_running_job_count": int(pending_running),
        "last_delivery_status": _status_str(last_delivery.status) if last_delivery else None,
        "last_delivery_at": _iso(last_delivery.sent_at or last_delivery.created_at) if last_delivery else None,
        "blocked_reasons": blocked_reasons,
        "safe_actions": _safe_account_actions(enabled, quarantined, readiness),
    }


def _safe_account_actions(enabled: bool, quarantined: bool, readiness: dict[str, Any]) -> list[str]:
    actions = ["inspect_detail", "eligibility_preview"]
    if readiness["freshness"] in ("stale", "expired", "missing", "transient"):
        actions.append("refresh_readiness")
    if quarantined:
        actions.append("inspect_quarantine")
    elif enabled:
        actions.append("disable_account")
    else:
        actions.append("enable_account")
    return actions


def build_account_detail(db: Session, account_id: int) -> Optional[dict[str, Any]]:
    account = db.get(Account, int(account_id)) if hasattr(db, "get") else (
        db.query(Account).filter(Account.id == int(account_id)).first()
    )
    if account is None:
        return None
    aid = int(account.id)
    op = compute_account_operational_state(db, account)
    readiness = _readiness_freshness(db, aid)
    bindings = []
    for b in db.query(AccountTargetBinding).filter(AccountTargetBinding.account_id == aid).all():
        target = db.get(ChatTarget, int(b.target_id)) if hasattr(db, "get") else (
            db.query(ChatTarget).filter(ChatTarget.id == b.target_id).first()
        )
        probe = (
            db.query(AccountTargetMembershipProbe)
            .filter(
                AccountTargetMembershipProbe.account_id == aid,
                AccountTargetMembershipProbe.target_id == b.target_id,
            )
            .order_by(AccountTargetMembershipProbe.checked_at.desc())
            .first()
        )
        bindings.append(
            {
                "binding_id": b.id,
                "target_id": b.target_id,
                "target_title": (target.title if target else None) or f"target_{b.target_id}",
                "can_post": bool(b.can_post),
                "membership_status": probe.status if probe else None,
                "permission_checked_at": _iso(probe.checked_at) if probe else None,
                "last_failure_reason": probe.message if probe else None,
            }
        )
    profile = db.query(ScheduleProfile).filter(ScheduleProfile.account_id == aid).first()
    rules = db.query(ScheduleRule).filter(ScheduleRule.account_id == aid).all()
    recent_jobs = (
        db.query(ScheduledJob)
        .filter(ScheduledJob.account_id == aid)
        .order_by(ScheduledJob.created_at.desc())
        .limit(20)
        .all()
    )
    recent_gw = (
        db.query(TelegramGatewayJob)
        .filter(TelegramGatewayJob.account_id == aid)
        .order_by(TelegramGatewayJob.created_at.desc())
        .limit(20)
        .all()
    )
    recent_deliveries = (
        db.query(MessageDelivery)
        .filter(MessageDelivery.account_id == aid)
        .order_by(MessageDelivery.created_at.desc())
        .limit(20)
        .all()
    )
    tier = (op.get("tier") or "").lower()
    return {
        "account": {
            "id": aid,
            "label": _account_label(account),
            "enabled": _status_str(account.status).lower() == "active",
            "quarantined": tier in ("reserved", "controller"),
            "tier": tier,
        },
        "session_authorization": {
            "authorized": readiness["display_status"] not in ("NOT_AUTHORIZED", "FAILED_AUTH"),
            "status": readiness["display_status"],
            "reason_code": readiness.get("failure_code"),
            "reason": readiness.get("reason"),
            "checked_at": readiness["checked_at"],
            "expires_at": readiness["expires_at"],
            "freshness": readiness["freshness"],
        },
        "readiness": readiness,
        "bindings": bindings,
        "schedule_profile": {
            "id": profile.id if profile else None,
            "enabled": bool(profile.is_enabled) if profile else False,
            "timezone": profile.timezone if profile else None,
        },
        "schedule_rules": [
            {
                "id": r.id,
                "type": r.type,
                "enabled": bool(r.is_enabled),
                "times_json": r.times_json,
            }
            for r in rules
        ],
        "recent_jobs": [_serialize_job(j) for j in recent_jobs],
        "recent_gateway_jobs": [_serialize_gateway_job(g) for g in recent_gw],
        "recent_deliveries": [_serialize_delivery(d) for d in recent_deliveries],
        "safe_actions": _safe_account_actions(
            _status_str(account.status).lower() == "active",
            tier in ("reserved", "controller"),
            readiness,
        ),
        "authorization_instruction": (
            "Session authorization requires the existing CLI/session workflow. "
            "This page shows state only; no session material is exposed."
            if readiness["display_status"] in ("NOT_AUTHORIZED", "FAILED_AUTH", "PROBE_REQUIRED")
            else None
        ),
    }


def _serialize_job(job: ScheduledJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "account_id": job.account_id,
        "target_id": job.target_id,
        "type": job.type,
        "status": _status_str(job.status),
        "run_at": _iso(job.run_at),
        "attempts": job.attempts,
        "last_error": job.last_error,
        "created_at": _iso(job.created_at),
        "updated_at": _iso(job.updated_at),
    }


def _serialize_gateway_job(row: TelegramGatewayJob) -> dict[str, Any]:
    result = row.result_json if isinstance(row.result_json, dict) else {}
    reconciled = result.get("reconciled") or result.get("p5d_reconciled")
    return {
        "id": row.id,
        "account_id": row.account_id,
        "scheduled_job_id": (row.payload_json or {}).get("scheduled_job_id")
        if isinstance(row.payload_json, dict)
        else None,
        "task_type": row.task_type,
        "target": row.target,
        "state": (row.status or "").lower(),
        "attempts": row.attempts,
        "error_code": row.error_code,
        "error_message": (row.error_message or "")[:500] if row.error_message else None,
        "reconciled": bool(reconciled),
        "claim_updated_at": _iso(row.updated_at),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        "state_explanation": _gateway_state_explanation(row),
    }


def _gateway_state_explanation(row: TelegramGatewayJob) -> str:
    st = (row.status or "").lower()
    result = row.result_json if isinstance(row.result_json, dict) else {}
    if result.get("reconciliation_required") or result.get("p5d_outcome") == "AMBIGUOUS_RECONCILIATION_REQUIRED":
        return "reconciliation required — inspect evidence before any resend"
    mapping = {
        "pending": "queued; gateway worker will claim when active and authorized",
        "running": "claimed by gateway worker",
        "done": "completed successfully or reconciled to terminal state",
        "retry": "retryable pre-send failure; not an automatic resend trigger",
        "failed": "terminal failure or policy denial — inspect error class",
    }
    return mapping.get(st, st or "unknown")


def _delivery_outcome_class(delivery: MessageDelivery) -> str:
    st = _status_str(delivery.status).upper()
    err = (delivery.error_code or "").upper()
    if st == "SENT" and delivery.tg_message_id:
        return "SENT_CONFIRMED"
    if st in ("FAILED", "ERROR") and "RECONCIL" in err:
        return "AMBIGUOUS_RECONCILIATION_REQUIRED"
    if st in ("FAILED", "ERROR") and err in ("POLICY", "DENIED", "NOT_ALLOWED"):
        return "TERMINAL_POLICY_DENIAL"
    if st in ("FAILED", "ERROR"):
        return "RETRYABLE_PRE_SEND_FAILURE"
    if st == "SENT" and not delivery.tg_message_id:
        return "AMBIGUOUS_RECONCILIATION_REQUIRED"
    if st in ("SKIPPED", "CANCELLED"):
        return "NOT_SENT_CONFIRMED"
    return st or "UNKNOWN"


def _serialize_delivery(d: MessageDelivery) -> dict[str, Any]:
    outcome = _delivery_outcome_class(d)
    return {
        "id": d.id,
        "job_id": d.job_id,
        "account_id": d.account_id,
        "target_id": d.target_id,
        "status": _status_str(d.status),
        "outcome_class": outcome,
        "tg_message_id": d.tg_message_id,
        "error_code": d.error_code,
        "error_message": (d.error_message or "")[:300] if d.error_message else None,
        "sent_at": _iso(d.sent_at),
        "created_at": _iso(d.created_at),
        "reconcile_allowed": outcome == AMBIGUOUS_RECONCILIATION_REQUIRED
        or (
            int(d.id) not in PROTECTED_DELIVERY_IDS
            and _status_str(d.status).upper() in ("FAILED", "ERROR", "SENDING")
            and not d.tg_message_id
        ),
        "protected_record": int(d.id) in PROTECTED_DELIVERY_IDS,
    }


def list_scheduled_jobs(
    db: Session,
    *,
    account_id: Optional[int] = None,
    status: Optional[str] = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    q = db.query(ScheduledJob).order_by(ScheduledJob.created_at.desc())
    if account_id:
        q = q.filter(ScheduledJob.account_id == int(account_id))
    if status:
        q = q.filter(ScheduledJob.status == status.upper())
    rows = q.limit(max(1, min(limit, 200))).all()
    out = []
    for job in rows:
        item = _serialize_job(job)
        gw = None
        payload_match = (
            db.query(TelegramGatewayJob)
            .filter(TelegramGatewayJob.account_id == job.account_id)
            .order_by(TelegramGatewayJob.id.desc())
            .limit(50)
            .all()
        )
        for g in payload_match:
            pj = g.payload_json if isinstance(g.payload_json, dict) else {}
            if pj.get("scheduled_job_id") == job.id:
                gw = _serialize_gateway_job(g)
                break
        delivery = (
            db.query(MessageDelivery)
            .filter(MessageDelivery.job_id == job.id)
            .order_by(MessageDelivery.id.desc())
            .first()
        )
        item["gateway_job"] = gw
        item["delivery"] = _serialize_delivery(delivery) if delivery else None
        item["cancel_allowed"] = _status_str(job.status).upper() == "PENDING"
        out.append(item)
    return out


def list_gateway_jobs(
    db: Session,
    *,
    account_id: Optional[int] = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    q = db.query(TelegramGatewayJob).order_by(TelegramGatewayJob.id.desc())
    if account_id:
        q = q.filter(TelegramGatewayJob.account_id == int(account_id))
    return [_serialize_gateway_job(r) for r in q.limit(max(1, min(limit, 200))).all()]


def list_deliveries_fleet(
    db: Session,
    *,
    account_id: Optional[int] = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    q = db.query(MessageDelivery).order_by(MessageDelivery.created_at.desc())
    if account_id:
        q = q.filter(MessageDelivery.account_id == int(account_id))
    return [_serialize_delivery(d) for d in q.limit(max(1, min(limit, 200))).all()]


def list_targets(db: Session, *, limit: int = 200) -> list[dict[str, Any]]:
    out = []
    for t in db.query(ChatTarget).order_by(ChatTarget.id.asc()).limit(limit).all():
        bound = (
            db.query(func.count(AccountTargetBinding.id))
            .filter(AccountTargetBinding.target_id == t.id)
            .scalar()
            or 0
        )
        can_post_true = (
            db.query(func.count(AccountTargetBinding.id))
            .filter(AccountTargetBinding.target_id == t.id, AccountTargetBinding.can_post.is_(True))
            .scalar()
            or 0
        )
        out.append(
            {
                "id": t.id,
                "peer_id": getattr(t, "peer_id", None) or getattr(t, "telegram_id", None),
                "type": getattr(t, "type", None) or getattr(t, "target_type", None),
                "title": t.title,
                "username": getattr(t, "username", None),
                "risk_tier": getattr(t, "risk_tier", None),
                "enabled": bool(getattr(t, "is_enabled", True)),
                "bound_account_count": int(bound),
                "can_post_summary": f"{can_post_true}/{bound} can_post",
            }
        )
    return out


def build_target_detail(db: Session, target_id: int) -> Optional[dict[str, Any]]:
    t = db.get(ChatTarget, int(target_id)) if hasattr(db, "get") else (
        db.query(ChatTarget).filter(ChatTarget.id == int(target_id)).first()
    )
    if t is None:
        return None
    bindings = []
    for b in db.query(AccountTargetBinding).filter(AccountTargetBinding.target_id == t.id).all():
        probe = (
            db.query(AccountTargetMembershipProbe)
            .filter(
                AccountTargetMembershipProbe.account_id == b.account_id,
                AccountTargetMembershipProbe.target_id == t.id,
            )
            .order_by(AccountTargetMembershipProbe.checked_at.desc())
            .first()
        )
        bindings.append(
            {
                "binding_id": b.id,
                "account_id": b.account_id,
                "can_post": bool(b.can_post),
                "membership_status": probe.status if probe else None,
                "permission_checked_at": _iso(probe.checked_at) if probe else None,
                "last_failure_reason": probe.message if probe else None,
            }
        )
    recent_jobs = (
        db.query(ScheduledJob)
        .filter(ScheduledJob.target_id == t.id)
        .order_by(ScheduledJob.created_at.desc())
        .limit(15)
        .all()
    )
    recent_deliveries = (
        db.query(MessageDelivery)
        .filter(MessageDelivery.target_id == t.id)
        .order_by(MessageDelivery.created_at.desc())
        .limit(15)
        .all()
    )
    return {
        "target": {
            "id": t.id,
            "peer_id": getattr(t, "peer_id", None) or getattr(t, "telegram_id", None),
            "title": t.title,
            "username": getattr(t, "username", None),
            "type": getattr(t, "type", None),
            "risk_tier": getattr(t, "risk_tier", None),
            "enabled": bool(getattr(t, "is_enabled", True)),
        },
        "bindings": bindings,
        "recent_jobs": [_serialize_job(j) for j in recent_jobs],
        "recent_deliveries": [_serialize_delivery(d) for d in recent_deliveries],
    }


def list_bindings(db: Session, *, limit: int = 200) -> list[dict[str, Any]]:
    out = []
    for b in db.query(AccountTargetBinding).order_by(AccountTargetBinding.id.asc()).limit(limit).all():
        probe = (
            db.query(AccountTargetMembershipProbe)
            .filter(
                AccountTargetMembershipProbe.account_id == b.account_id,
                AccountTargetMembershipProbe.target_id == b.target_id,
            )
            .order_by(AccountTargetMembershipProbe.checked_at.desc())
            .first()
        )
        schedule_count = (
            db.query(func.count(ScheduleRule.id))
            .filter(ScheduleRule.account_id == b.account_id, ScheduleRule.is_enabled.is_(True))
            .scalar()
            or 0
        )
        out.append(
            {
                "binding_id": b.id,
                "account_id": b.account_id,
                "target_id": b.target_id,
                "can_post": bool(b.can_post),
                "membership_status": probe.status if probe else None,
                "permission_checked_at": _iso(probe.checked_at) if probe else None,
                "last_failure_reason": probe.message if probe else None,
                "linked_schedule_count": int(schedule_count),
                "refresh_allowed": True,
            }
        )
    return out


def build_schedule_eligibility_rows(db: Session) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for rule in db.query(ScheduleRule).order_by(ScheduleRule.account_id.asc()).all():
        profile = (
            db.query(ScheduleProfile)
            .filter(ScheduleProfile.account_id == rule.account_id)
            .first()
        )
        target_ids: list[int] = []
        if (rule.target_mode or "").upper() == "ONLY_SELECTED" and rule.selected_target_ids_json:
            import json

            try:
                target_ids = [int(x) for x in json.loads(rule.selected_target_ids_json)]
            except Exception:
                target_ids = []
        else:
            target_ids = [
                int(b.target_id)
                for b in db.query(AccountTargetBinding)
                .filter(AccountTargetBinding.account_id == rule.account_id)
                .all()
            ]
        if not target_ids:
            target_ids = [0]
        for tid in target_ids[:5]:
            decision = evaluate_generation_eligibility(
                db,
                job_type=rule.type or "PROMO",
                account_id=int(rule.account_id),
                target_id=int(tid) if tid else 0,
                generation_scope="normal",
                generator_date=date.today(),
                schedule_rule_id=int(rule.id),
                schedule_profile_id=int(profile.id) if profile else None,
            )
            readiness = _readiness_freshness(db, int(rule.account_id))
            dup = same_day_duplicate_status(
                db,
                account_id=int(rule.account_id),
                target_id=int(tid) if tid else 0,
                job_type=rule.type or "PROMO",
                timezone_name=profile.timezone if profile else None,
            )
            nxt = compute_next_occurrence(rule, profile)
            rows.append(
                {
                    "profile_id": profile.id if profile else None,
                    "rule_id": rule.id,
                    "account_id": rule.account_id,
                    "target_id": tid if tid else None,
                    "timezone": profile.timezone if profile else None,
                    "rule_enabled": bool(rule.is_enabled),
                    "profile_enabled": bool(profile.is_enabled) if profile else False,
                    "generation_mode": promo_generation_mode(),
                    "eligible": decision.allowed,
                    "reason_code": decision.reason_code,
                    "human_reason": decision.human_reason,
                    "readiness_freshness": readiness["freshness"],
                    "no_go_active": production_certified_no_go_active(),
                    "next_occurrence": nxt,
                    "same_day_duplicate": dup,
                }
            )
    return rows


def build_system_safety_snapshot(db: Session) -> dict[str, Any]:
    env_vals = {k: os.environ.get(k, "") for k in _DANGEROUS_ENV_KEYS}
    expected = {
        "AUTOSTORY_PRODUCTION_CERTIFIED_NO_GO": "true",
        "PROMO_GENERATION_MODE": "disabled",
        "SCHEDULER_MUTATIONS_ENABLED": "false",
        "P5C_SINGLE_SEND_ENABLED": "false",
        "P5D_SINGLE_SEND_ENABLED": "false",
        "P5D_CERTIFICATION_MODE": "",
        "P5D_FAILPOINT": "",
    }
    flags = []
    for key in _DANGEROUS_ENV_KEYS:
        raw = (env_vals.get(key) or "").strip()
        exp = expected.get(key, "")
        match = (raw.lower() == exp.lower()) if exp else (not raw)
        dangerous = not match and key in (
            "AUTOSTORY_PRODUCTION_CERTIFIED_NO_GO",
            "PROMO_GENERATION_MODE",
            "SCHEDULER_MUTATIONS_ENABLED",
            "P5C_SINGLE_SEND_ENABLED",
            "P5D_SINGLE_SEND_ENABLED",
            "P5D_CERTIFICATION_MODE",
            "P5D_FAILPOINT",
        )
        if key == "AUTOSTORY_PRODUCTION_CERTIFIED_NO_GO" and raw.lower() not in ("true", "1", "yes", "on"):
            dangerous = True
        flags.append(
            {
                "setting": key,
                "env_value": raw or "(unset)",
                "expected": exp or "(unset)",
                "match": match,
                "dangerous": dangerous,
            }
        )
    queue = {
        str(s): int(c)
        for s, c in db.query(ScheduledJob.status, func.count(ScheduledJob.id)).group_by(ScheduledJob.status).all()
    }
    recon_required = (
        db.query(func.count(MessageDelivery.id))
        .filter(MessageDelivery.error_code.ilike("%reconcil%"))
        .scalar()
        or 0
    )
    stale_readiness = 0
    fresh_ready = 0
    not_authorized_count = 0
    failed_auth_count = 0
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for snap in db.query(AccountReadinessSnapshot).all():
        if snap.status == readiness_store.STAT_READY:
            if readiness_store.snapshot_ready_and_valid(db, int(snap.account_id)):
                fresh_ready += 1
            else:
                stale_readiness += 1
        elif snap.status == readiness_store.STAT_NOT_AUTH:
            not_authorized_count += 1
            if (snap.failure_code or "").lower() in ("failed_auth", "unauthorized_session"):
                failed_auth_count += 1
    quarantined = 0
    for account in db.query(Account).all():
        op = compute_account_operational_state(db, account)
        if (op.get("tier") or "").lower() in ("reserved", "controller"):
            quarantined += 1
    worker_runtime = read_runtime_status()
    cycle_sleep = float(worker_runtime.get("next_cycle_sleep_sec") or 60)
    status_age_sec: Optional[float] = None
    updated = worker_runtime.get("updated_at")
    if updated:
        try:
            u = datetime.fromisoformat(str(updated))
            status_age_sec = (now - u).total_seconds()
        except ValueError:
            pass
    cycle_duration = float(worker_runtime.get("cycle_duration_sec") or 0)
    warnings: list[dict[str, Any]] = []
    for svc in ("telegram-gateway", "kathleen-account-listener", "storyfleet-bot"):
        active = _svc_active(svc)
        enabled = _svc_enabled(svc)
        if active == "active":
            warnings.append({"code": f"{svc}_active", "message": f"{svc} is active during NO_GO soak"})
        if enabled == "enabled":
            warnings.append({"code": f"{svc}_enabled", "message": f"{svc} is enabled (may auto-start on boot)"})
    if not (os.environ.get("P5D_SINGLE_SEND_ENABLED") or "").strip():
        warnings.append({"code": "p5d_unset", "message": "P5D_SINGLE_SEND_ENABLED unset (code defaults false)"})
    if status_age_sec is not None and status_age_sec > cycle_sleep * 2:
        warnings.append({"code": "worker_status_stale", "message": f"readiness worker status file age {int(status_age_sec)}s"})
    if cycle_duration > float(os.environ.get("READINESS_WORKER_SLOW_CYCLE_WARN_SEC", "120")):
        warnings.append({
            "code": "slow_cycle",
            "message": f"last cycle duration {cycle_duration}s exceeds threshold",
            "selection_duration_sec": worker_runtime.get("selection_duration_sec"),
            "probe_duration_sec": worker_runtime.get("probe_duration_sec"),
        })
    return {
        "flags": flags,
        "services": {
            "autostory-web": _svc_active("autostory-web"),
            "autostory-scheduler": _svc_active("autostory-scheduler"),
            "telegram-gateway": _svc_active("telegram-gateway"),
            "autostory-readiness-worker": _svc_active("autostory-readiness-worker"),
            "kathleen-account-listener": _svc_active("kathleen-account-listener"),
            "storyfleet-bot": _svc_active("storyfleet-bot"),
        },
        "service_enablement": {
            "telegram-gateway": _svc_enabled("telegram-gateway"),
            "kathleen-account-listener": _svc_enabled("kathleen-account-listener"),
            "storyfleet-bot": _svc_enabled("storyfleet-bot"),
            "autostory-readiness-worker": _svc_enabled("autostory-readiness-worker"),
        },
        "warnings": warnings,
        "queue_counts": queue,
        "reconciliation_required_count": int(recon_required),
        "quarantined_account_count": int(quarantined),
        "stale_readiness_count": int(stale_readiness),
        "fresh_readiness_count": int(fresh_ready),
        "not_authorized_readiness_count": int(not_authorized_count),
        "failed_auth_readiness_count": int(failed_auth_count),
        "readiness_worker": {
            "systemd_active": _svc_active("autostory-readiness-worker"),
            "last_cycle_id": worker_runtime.get("cycle_id"),
            "last_cycle_at": worker_runtime.get("updated_at"),
            "last_cycle_duration_sec": worker_runtime.get("cycle_duration_sec"),
            "selection_duration_sec": worker_runtime.get("selection_duration_sec"),
            "probe_duration_sec": worker_runtime.get("probe_duration_sec"),
            "status_age_sec": status_age_sec,
            "last_checked": worker_runtime.get("checked"),
            "last_ready": worker_runtime.get("ready"),
            "last_not_authorized": worker_runtime.get("not_authorized"),
            "last_error": worker_runtime.get("last_error"),
            "next_cycle_sleep_sec": worker_runtime.get("next_cycle_sleep_sec"),
            "dry_run": worker_runtime.get("dry_run"),
            "allow_ids": worker_runtime.get("allow_ids"),
        },
        "pending_gateway_jobs": (
            db.query(func.count(TelegramGatewayJob.id))
            .filter(TelegramGatewayJob.status.in_(("pending", "retry")))
            .scalar()
            or 0
        ),
        "build_info": build_info_snapshot(),
    }


def queue_counts_snapshot(db: Session) -> dict[str, int]:
    return {
        "scheduled_jobs": int(db.query(func.count(ScheduledJob.id)).scalar() or 0),
        "gateway_jobs": int(db.query(func.count(TelegramGatewayJob.id)).scalar() or 0),
        "deliveries": int(db.query(func.count(MessageDelivery.id)).scalar() or 0),
        "pending_jobs": int(
            db.query(func.count(ScheduledJob.id)).filter(ScheduledJob.status == "PENDING").scalar() or 0
        ),
        "running_jobs": int(
            db.query(func.count(ScheduledJob.id)).filter(ScheduledJob.status == "RUNNING").scalar() or 0
        ),
        "max_job_id": int(db.query(func.max(ScheduledJob.id)).scalar() or 0),
        "max_delivery_id": int(db.query(func.max(MessageDelivery.id)).scalar() or 0),
        "max_gateway_job_id": int(db.query(func.max(TelegramGatewayJob.id)).scalar() or 0),
    }
