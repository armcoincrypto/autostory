"""
P6 — Operator control read models (read-only aggregation for dashboard pages).

Reuses canonical readiness, eligibility, and scheduler models. No execution side effects.
"""
from __future__ import annotations

import os
import subprocess
from datetime import date, datetime, timezone
from typing import Any, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.clients import readiness_store
from src.core.account_operational_state import compute_account_operational_state
from src.core.datetime_utc import to_utc_iso_z
from src.core.models import Account
from src.core.scheduler_models import (
    AccountReadinessSnapshot,
    AccountTargetBinding,
    AccountTargetMembershipProbe,
    ChatTarget,
    MessageDelivery,
    ScheduleProfile,
    ScheduleRule,
    ScheduledJob,
)
from src.scheduler.generation_eligibility import (
    evaluate_generation_eligibility,
    production_certified_no_go_active,
    promo_generation_mode,
)
from src.telegram_gateway.models import TelegramGatewayJob

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
    return {
        "id": d.id,
        "job_id": d.job_id,
        "account_id": d.account_id,
        "target_id": d.target_id,
        "status": _status_str(d.status),
        "outcome_class": _delivery_outcome_class(d),
        "tg_message_id": d.tg_message_id,
        "error_code": d.error_code,
        "error_message": (d.error_message or "")[:300] if d.error_message else None,
        "sent_at": _iso(d.sent_at),
        "created_at": _iso(d.created_at),
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
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for snap in db.query(AccountReadinessSnapshot).all():
        if snap.status == readiness_store.STAT_READY and not readiness_store.snapshot_ready_and_valid(
            db, int(snap.account_id)
        ):
            stale_readiness += 1
    quarantined = 0
    for account in db.query(Account).all():
        op = compute_account_operational_state(db, account)
        if (op.get("tier") or "").lower() in ("reserved", "controller"):
            quarantined += 1
    return {
        "flags": flags,
        "services": {
            "autostory-web": _svc_active("autostory-web"),
            "autostory-scheduler": _svc_active("autostory-scheduler"),
            "telegram-gateway": _svc_active("telegram-gateway"),
        },
        "queue_counts": queue,
        "reconciliation_required_count": int(recon_required),
        "quarantined_account_count": int(quarantined),
        "stale_readiness_count": int(stale_readiness),
        "pending_gateway_jobs": (
            db.query(func.count(TelegramGatewayJob.id))
            .filter(TelegramGatewayJob.status.in_(("pending", "retry")))
            .scalar()
            or 0
        ),
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
