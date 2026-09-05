"""Owner-facing presentation for Scheduler jobs (Wave 5).

Read-only mapping over ``scheduled_jobs`` / related rows. Does not call Telegram,
mutate jobs, or change scheduler worker/generator semantics.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from sqlalchemy.orm import Session

from src.core.datetime_utc import to_utc_iso_z
from src.core.models import Account
from src.core.scheduler_models import (
    ChatTarget,
    JobStatus,
    MessageDelivery,
    MessageType,
    ScheduledJob,
)
from src.dashboard.scheduler_mutations import scheduler_mutations_enabled
from src.scheduler.generation_eligibility import promo_generation_mode

STATUS_OWNER_LABELS: dict[str, str] = {
    JobStatus.PENDING.value: "Scheduled",
    JobStatus.RUNNING.value: "In progress",
    JobStatus.SENT.value: "Sent",
    JobStatus.FAILED.value: "Failed",
    JobStatus.SKIPPED.value: "Skipped",
    JobStatus.CANCELLED.value: "Cancelled",
}

TYPE_OWNER_LABELS: dict[str, str] = {
    MessageType.PROMO.value: "Promotional message",
    MessageType.INFO.value: "Information message",
}

UPCOMING_STATUSES = (JobStatus.PENDING.value, JobStatus.RUNNING.value)
TERMINAL_STATUSES = (
    JobStatus.SENT.value,
    JobStatus.FAILED.value,
    JobStatus.SKIPPED.value,
    JobStatus.CANCELLED.value,
)

OWNER_FILTERS = ("all", "upcoming", "sent", "failed", "cancelled", "skipped")

RECENT_LIMIT_DEFAULT = 40
FAILED_LIMIT_DEFAULT = 25
UPCOMING_LIMIT_DEFAULT = 50


def map_job_status(raw: Optional[str]) -> dict[str, str]:
    key = (raw or "").strip().upper() or "UNKNOWN"
    label = STATUS_OWNER_LABELS.get(key)
    if label:
        return {"raw": key, "label": label, "known": "true"}
    return {"raw": key or "UNKNOWN", "label": "Unknown status", "known": "false"}


def map_job_type(raw: Optional[str]) -> dict[str, str]:
    key = (raw or "").strip().upper() or "UNKNOWN"
    label = TYPE_OWNER_LABELS.get(key)
    if label:
        return {"raw": key, "label": label, "known": "true"}
    return {"raw": key, "label": key.title() if key != "UNKNOWN" else "Unknown type", "known": "false"}


def format_display_utc(dt: Optional[datetime]) -> str:
    """Human display for naive-UTC (or aware) instants — never shifts wall clock."""
    iso = to_utc_iso_z(dt)
    if not iso:
        return "—"
    # 2026-07-14T22:55:44.711192Z → 2026-07-14 22:55 UTC
    core = iso.rstrip("Z").split(".")[0].replace("T", " ")
    if len(core) >= 16:
        core = core[:16]
    return f"{core} UTC"


def owner_short_reason(last_error: Optional[str], *, status: Optional[str] = None) -> str:
    """Owner-safe short reason; hide internal markers and truncate."""
    raw = (last_error or "").strip()
    if not raw:
        return ""
    if raw.startswith("__") and raw.endswith("__"):
        # Operator/certification markers stored in last_error — not a user failure.
        return ""
    if raw.startswith("execution_guard:"):
        return "Blocked by safety guard"
    if "scheduler_mutations_disabled" in raw:
        return "Scheduling changes are disabled"
    # Prefer first line / short snippet
    line = raw.splitlines()[0].strip()
    if len(line) > 140:
        return line[:137] + "…"
    return line


def _account_label(account: Optional[dict[str, Any]], account_id: int) -> str:
    if not account:
        return f"Account #{account_id}"
    username = account.get("username")
    if username:
        u = username if str(username).startswith("@") else f"@{username}"
        return u
    if account.get("first_name"):
        return str(account["first_name"])
    phone = account.get("phone_number")
    if phone:
        return str(phone)
    return f"Account #{account_id}"


def _target_label(target: Optional[dict[str, Any]], target_id: int) -> str:
    if not target:
        return f"Target #{target_id}"
    if target.get("title"):
        return str(target["title"])
    username = target.get("username")
    if username:
        u = username if str(username).startswith("@") else f"@{username}"
        return u
    return f"Target #{target_id}"


def _batch_accounts(db: Session, ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    """Load account label fields only — never select session material columns."""
    uniq = sorted({int(i) for i in ids if i is not None})
    if not uniq:
        return {}
    rows = (
        db.query(
            Account.id,
            Account.username,
            Account.first_name,
            Account.phone_number,
        )
        .filter(Account.id.in_(uniq))
        .all()
    )
    return {
        int(row.id): {
            "id": int(row.id),
            "username": row.username,
            "first_name": row.first_name,
            "phone_number": row.phone_number,
        }
        for row in rows
    }


def _batch_targets(db: Session, ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    uniq = sorted({int(i) for i in ids if i is not None})
    if not uniq:
        return {}
    rows = (
        db.query(
            ChatTarget.id,
            ChatTarget.title,
            ChatTarget.username,
            ChatTarget.chat_type,
        )
        .filter(ChatTarget.id.in_(uniq))
        .all()
    )
    return {
        int(row.id): {
            "id": int(row.id),
            "title": row.title,
            "username": row.username,
            "chat_type": row.chat_type,
        }
        for row in rows
    }


def _batch_latest_deliveries(db: Session, job_ids: Iterable[int]) -> dict[int, MessageDelivery]:
    """One latest delivery row per job_id (batched; no N+1)."""
    uniq = sorted({int(i) for i in job_ids if i is not None})
    if not uniq:
        return {}
    rows = (
        db.query(MessageDelivery)
        .filter(MessageDelivery.job_id.in_(uniq))
        .order_by(MessageDelivery.job_id.asc(), MessageDelivery.id.desc())
        .all()
    )
    out: dict[int, MessageDelivery] = {}
    for d in rows:
        jid = int(d.job_id) if d.job_id is not None else None
        if jid is None or jid in out:
            continue
        out[jid] = d
    return out


def present_job(
    job: ScheduledJob,
    *,
    accounts: dict[int, dict[str, Any]],
    targets: dict[int, dict[str, Any]],
    deliveries: dict[int, MessageDelivery],
) -> dict[str, Any]:
    status = map_job_status(getattr(job, "status", None))
    jtype = map_job_type(getattr(job, "type", None))
    aid = int(job.account_id)
    tid = int(job.target_id)
    account = accounts.get(aid)
    target = targets.get(tid)
    delivery = deliveries.get(int(job.id))
    reason = owner_short_reason(getattr(job, "last_error", None), status=status["raw"])
    if not reason and delivery is not None:
        reason = owner_short_reason(getattr(delivery, "error_message", None))

    return {
        "job_id": int(job.id),
        "scheduled_at": format_display_utc(job.run_at),
        "scheduled_at_iso": to_utc_iso_z(job.run_at),
        "created_at": format_display_utc(getattr(job, "created_at", None)),
        "created_at_iso": to_utc_iso_z(getattr(job, "created_at", None)),
        "updated_at": format_display_utc(getattr(job, "updated_at", None)),
        "account_id": aid,
        "account_label": _account_label(account, aid),
        "type_raw": jtype["raw"],
        "type_label": jtype["label"],
        "target_id": tid,
        "target_label": _target_label(target, tid),
        "target_chat_type": (target or {}).get("chat_type"),
        "status_raw": status["raw"],
        "status_label": status["label"],
        "status_known": status["known"] == "true",
        "result_label": status["label"],
        "short_reason": reason,
        "attempts": int(getattr(job, "attempts", 0) or 0),
        "delivery": None
        if delivery is None
        else {
            "status": getattr(delivery, "status", None),
            "sent_at": format_display_utc(getattr(delivery, "sent_at", None)),
            "sent_at_iso": to_utc_iso_z(getattr(delivery, "sent_at", None)),
            "error_code": getattr(delivery, "error_code", None),
            "error_message": owner_short_reason(getattr(delivery, "error_message", None)),
        },
        "diagnostics": {
            "raw_status": status["raw"],
            "lease_until": format_display_utc(getattr(job, "lease_until", None)),
            "lease_owner": getattr(job, "lease_owner", None),
            "template_id": getattr(job, "template_id", None),
            "last_error_raw": (getattr(job, "last_error", None) or "")[:500],
        },
    }


def _present_many(
    jobs: list[ScheduledJob],
    *,
    accounts: dict[int, dict[str, Any]],
    targets: dict[int, dict[str, Any]],
    deliveries: dict[int, MessageDelivery],
) -> list[dict[str, Any]]:
    return [
        present_job(j, accounts=accounts, targets=targets, deliveries=deliveries) for j in jobs
    ]


def build_owner_scheduler_view(
    db: Session,
    *,
    filter_key: str = "all",
    upcoming_limit: int = UPCOMING_LIMIT_DEFAULT,
    recent_limit: int = RECENT_LIMIT_DEFAULT,
    failed_limit: int = FAILED_LIMIT_DEFAULT,
) -> dict[str, Any]:
    """Assemble owner Scheduler page model from DB only (no Telegram)."""
    filt = (filter_key or "all").strip().lower()
    if filt not in OWNER_FILTERS:
        filt = "all"

    pending_n = (
        db.query(ScheduledJob).filter(ScheduledJob.status == JobStatus.PENDING.value).count()
    )
    running_n = (
        db.query(ScheduledJob).filter(ScheduledJob.status == JobStatus.RUNNING.value).count()
    )
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
    failed_recent_n = (
        db.query(ScheduledJob)
        .filter(ScheduledJob.status == JobStatus.FAILED.value, ScheduledJob.run_at >= since)
        .count()
    )

    upcoming_q = (
        db.query(ScheduledJob)
        .filter(ScheduledJob.status.in_(list(UPCOMING_STATUSES)))
        .order_by(ScheduledJob.run_at.asc())
        .limit(max(1, min(upcoming_limit, 200)))
    )
    upcoming_jobs = upcoming_q.all()

    recent_q = (
        db.query(ScheduledJob)
        .filter(ScheduledJob.status.in_(list(TERMINAL_STATUSES)))
        .order_by(ScheduledJob.run_at.desc())
        .limit(max(1, min(recent_limit, 200)))
    )
    recent_jobs = recent_q.all()

    failed_q = (
        db.query(ScheduledJob)
        .filter(ScheduledJob.status == JobStatus.FAILED.value)
        .order_by(ScheduledJob.run_at.desc())
        .limit(max(1, min(failed_limit, 200)))
    )
    failed_jobs = failed_q.all()

    # Optional filtered list for single-table mode
    filtered_jobs: list[ScheduledJob] = []
    if filt == "upcoming":
        filtered_jobs = upcoming_jobs
    elif filt == "sent":
        filtered_jobs = (
            db.query(ScheduledJob)
            .filter(ScheduledJob.status == JobStatus.SENT.value)
            .order_by(ScheduledJob.run_at.desc())
            .limit(recent_limit)
            .all()
        )
    elif filt == "failed":
        filtered_jobs = failed_jobs
    elif filt == "cancelled":
        filtered_jobs = (
            db.query(ScheduledJob)
            .filter(ScheduledJob.status == JobStatus.CANCELLED.value)
            .order_by(ScheduledJob.run_at.desc())
            .limit(recent_limit)
            .all()
        )
    elif filt == "skipped":
        filtered_jobs = (
            db.query(ScheduledJob)
            .filter(ScheduledJob.status == JobStatus.SKIPPED.value)
            .order_by(ScheduledJob.run_at.desc())
            .limit(recent_limit)
            .all()
        )

    all_jobs = list({j.id: j for j in (*upcoming_jobs, *recent_jobs, *failed_jobs, *filtered_jobs)}.values())
    account_ids = [j.account_id for j in all_jobs]
    target_ids = [j.target_id for j in all_jobs]
    job_ids = [j.id for j in all_jobs]
    accounts = _batch_accounts(db, account_ids)
    targets = _batch_targets(db, target_ids)
    deliveries = _batch_latest_deliveries(db, job_ids)

    mutations = bool(scheduler_mutations_enabled())
    gen_mode = (promo_generation_mode() or "disabled").strip().lower()
    generation_on = gen_mode not in {"", "disabled", "planning_only"}

    return {
        "filter": filt,
        "filters": list(OWNER_FILTERS),
        "mutations_enabled": mutations,
        "generation_enabled": generation_on,
        "generation_mode": gen_mode,
        "summary": {
            "scheduler_label": "Running",
            "upcoming": int(pending_n + running_n),
            "pending": int(pending_n),
            "running": int(running_n),
            "failed_recent": int(failed_recent_n),
        },
        "upcoming": _present_many(
            upcoming_jobs, accounts=accounts, targets=targets, deliveries=deliveries
        ),
        "recent": _present_many(
            recent_jobs, accounts=accounts, targets=targets, deliveries=deliveries
        ),
        "failed": _present_many(
            failed_jobs, accounts=accounts, targets=targets, deliveries=deliveries
        ),
        "filtered": _present_many(
            filtered_jobs, accounts=accounts, targets=targets, deliveries=deliveries
        ),
        "timezone_note": (
            "Times are shown in UTC. Jobs are stored as naive UTC instants; "
            "profile timezones (Asia/Yerevan default / Europe/Moscow in setup UI) "
            "affect generation only and are not changed in Wave 5."
        ),
        "external_telegram_calls_on_page_load": 0,
    }
