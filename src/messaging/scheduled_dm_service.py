"""Scheduled private Telegram messages via the existing Scheduler queue (Wave 10).

Creates PENDING ``ScheduledJob`` rows with ``type=DM``. Execution is handled by
the scheduler worker → ``OwnerDirectMessageService.send_now`` with a stable
idempotency key ``scheduled-dm:<job-id>``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

import structlog
from sqlalchemy.orm import Session

from src.core.datetime_utc import utc_now_naive
from src.core.scheduler_models import JobStatus, MessageType, ScheduledJob, ScheduleProfile
from src.messaging.owner_dm_service import (
    OwnerDirectMessageService,
    owner_message_for_eligibility,
    validate_dm_message,
    validate_peer,
)
from src.messaging.eligibility import evaluate_dm_account_eligibility
from src.messaging.scheduled_dm_flags import scheduled_dm_create_allowed, scheduled_dm_deny_payload
from src.scheduler.timezone import (
    DEFAULT_OWNER_TIMEZONE,
    SchedulerTimezoneError,
    owner_schedule_fields,
    parse_owner_local_datetime,
    resolve_owner_timezone,
)

logger = structlog.get_logger(__name__)

# Reject schedules already in the past (UTC). Small operational grace only.
PAST_SCHEDULE_GRACE_SEC = 30


def scheduled_dm_idempotency_key(job_id: int) -> str:
    """Stable OwnerDmIntent key for a scheduler job — never regenerate on retry."""
    return f"scheduled-dm:{int(job_id)}"


@dataclass(frozen=True)
class ScheduleDmResult:
    ok: bool
    status_code: int
    payload: dict[str, Any]


class ScheduledDirectMessageService:
    """Validate + persist scheduled DM jobs. Does not send Telegram messages."""

    def __init__(self, dm_service: Optional[OwnerDirectMessageService] = None) -> None:
        self._dm = dm_service or OwnerDirectMessageService()

    def _resolve_timezone(self, db: Session, account_id: int, timezone: Optional[str]) -> str:
        explicit = (timezone or "").strip()
        if explicit:
            return resolve_owner_timezone(explicit)
        profile = (
            db.query(ScheduleProfile)
            .filter(ScheduleProfile.account_id == int(account_id))
            .first()
        )
        if profile and (profile.timezone or "").strip():
            return resolve_owner_timezone(profile.timezone)
        return DEFAULT_OWNER_TIMEZONE

    def schedule(
        self,
        db: Session,
        *,
        account_id: int,
        peer_id: str,
        message: str,
        local_date: Optional[str] = None,
        local_time: Optional[str] = None,
        timezone: Optional[str] = None,
        peer_type: str = "private",
        dry_run_only: bool = False,
    ) -> ScheduleDmResult:
        if not scheduled_dm_create_allowed():
            return ScheduleDmResult(
                ok=False,
                status_code=423,
                payload=scheduled_dm_deny_payload(),
            )

        ok_msg, msg_code, msg_reason = validate_dm_message(message)
        if not ok_msg:
            return ScheduleDmResult(
                ok=False,
                status_code=400,
                payload={
                    "ok": False,
                    "error": msg_code,
                    "error_code": msg_code,
                    "message": msg_reason,
                },
            )
        ok_peer, peer_code, peer_reason = validate_peer(peer_id, peer_type)
        if not ok_peer:
            return ScheduleDmResult(
                ok=False,
                status_code=400,
                payload={
                    "ok": False,
                    "error": peer_code,
                    "error_code": peer_code,
                    "message": peer_reason,
                },
            )

        eligibility = evaluate_dm_account_eligibility(db, int(account_id))
        if not eligibility.eligible:
            err_code, err_msg = owner_message_for_eligibility(
                eligibility.code, eligibility.reason
            )
            return ScheduleDmResult(
                ok=False,
                status_code=403,
                payload={
                    "ok": False,
                    "error": err_code,
                    "error_code": err_code,
                    "message": err_msg,
                    "eligibility": eligibility.to_dict(),
                },
            )

        try:
            tz_name = self._resolve_timezone(db, int(account_id), timezone)
        except SchedulerTimezoneError as e:
            return ScheduleDmResult(
                ok=False,
                status_code=400,
                payload={
                    "ok": False,
                    "error": e.code if hasattr(e, "code") else "INVALID_TIMEZONE",
                    "error_code": getattr(e, "code", None) or "INVALID_TIMEZONE",
                    "message": str(e),
                },
            )

        if not (local_date and local_time):
            return ScheduleDmResult(
                ok=False,
                status_code=400,
                payload={
                    "ok": False,
                    "error": "INVALID_DATETIME",
                    "error_code": "INVALID_DATETIME",
                    "message": "local_date and local_time are required.",
                },
            )

        try:
            instant = parse_owner_local_datetime(
                str(local_date).strip(),
                str(local_time).strip(),
                tz_name,
            )
        except SchedulerTimezoneError as e:
            return ScheduleDmResult(
                ok=False,
                status_code=400,
                payload={
                    "ok": False,
                    "error": getattr(e, "code", None) or "INVALID_DATETIME",
                    "error_code": getattr(e, "code", None) or "INVALID_DATETIME",
                    "message": str(e),
                },
            )

        now = utc_now_naive()
        grace = timedelta(seconds=PAST_SCHEDULE_GRACE_SEC)
        if instant.utc_naive < (now - grace):
            return ScheduleDmResult(
                ok=False,
                status_code=400,
                payload={
                    "ok": False,
                    "error": "SCHEDULE_IN_PAST",
                    "error_code": "SCHEDULE_IN_PAST",
                    "message": "Schedule time is in the past. Choose a future local time.",
                    "scheduled_at_utc": instant.utc_naive.isoformat() + "Z",
                    "now_utc": now.isoformat() + "Z",
                },
            )

        # Schedule-time dry-run (eligibility/rate/peer/message). Kill switch may be
        # off at schedule time — execution re-checks MESSAGES_EXECUTION_ENABLED.
        dry = self._dm.dry_run(
            db,
            account_id=int(account_id),
            peer_id=(peer_id or "").strip(),
            text=str(message),
            peer_type=(peer_type or "private").strip().lower(),
        )
        elig = dry.get("eligibility") or {}
        rate = dry.get("rate_limit_result") or {}
        validation = dry.get("validation") or {}
        schedule_ok = (
            bool(validation.get("message_ok", True))
            and bool(validation.get("peer_ok", True))
            and bool(elig.get("eligible"))
            and bool(rate.get("allowed", True))
        )
        if not schedule_ok:
            return ScheduleDmResult(
                ok=False,
                status_code=400,
                payload={
                    "ok": False,
                    "error": "SCHEDULE_VALIDATION_FAILED",
                    "error_code": "SCHEDULE_VALIDATION_FAILED",
                    "message": dry.get("reason") or "Message cannot be scheduled.",
                    "dry_run": dry,
                },
            )

        fields = owner_schedule_fields(instant.utc_naive, tz_name)
        preview_payload = {
            "ok": True,
            "would_schedule": True,
            "account_id": int(account_id),
            "peer": (peer_id or "").strip(),
            "peer_type": (peer_type or "private").strip().lower(),
            "timezone": tz_name,
            "local_date": str(local_date).strip(),
            "local_time": str(local_time).strip(),
            "scheduled_at_utc": fields["scheduled_at_utc"],
            "scheduled_at_local": fields["scheduled_at_local"],
            "message_preview": (str(message).strip()[:120] + ("…" if len(str(message).strip()) > 120 else "")),
            "dry_run": dry,
            "transport_send_count": 0,
        }
        if dry_run_only:
            return ScheduleDmResult(ok=True, status_code=200, payload=preview_payload)

        now = utc_now_naive()
        job = ScheduledJob(
            account_id=int(account_id),
            target_id=None,
            type=MessageType.DM.value,
            run_at=instant.utc_naive,
            status=JobStatus.PENDING.value,
            template_id=None,
            attempts=0,
            last_error=None,
            peer_id=(peer_id or "").strip(),
            peer_type=(peer_type or "private").strip().lower(),
            message_body=str(message),
            schedule_timezone=tz_name,
            created_at=now,
            updated_at=now,
        )
        db.add(job)
        db.flush()
        job_id = int(job.id)
        idem = scheduled_dm_idempotency_key(job_id)
        db.commit()
        db.refresh(job)

        logger.info(
            "scheduled_dm_created",
            job_id=job_id,
            account_id=int(account_id),
            peer_id=job.peer_id,
            run_at=instant.utc_naive.isoformat(),
            timezone=tz_name,
            idempotency_key=idem,
        )
        return ScheduleDmResult(
            ok=True,
            status_code=200,
            payload={
                "ok": True,
                "job_id": job_id,
                "status": JobStatus.PENDING.value,
                "status_label": "Upcoming",
                "type": MessageType.DM.value,
                "account_id": int(account_id),
                "peer": job.peer_id,
                "peer_type": job.peer_type,
                "timezone": tz_name,
                "scheduled_at_utc": fields["scheduled_at_utc"],
                "scheduled_at_local": fields["scheduled_at_local"],
                "idempotency_key": idem,
                "message_preview": preview_payload["message_preview"],
                "transport_send_count": 0,
            },
        )

    def cancel(self, db: Session, *, job_id: int) -> ScheduleDmResult:
        if not scheduled_dm_create_allowed():
            return ScheduleDmResult(
                ok=False,
                status_code=423,
                payload=scheduled_dm_deny_payload(),
            )
        job = db.query(ScheduledJob).filter(ScheduledJob.id == int(job_id)).first()
        if not job or str(job.type).upper() != MessageType.DM.value:
            return ScheduleDmResult(
                ok=False,
                status_code=404,
                payload={
                    "ok": False,
                    "error": "NOT_FOUND",
                    "error_code": "NOT_FOUND",
                    "message": "Scheduled DM not found.",
                },
            )
        if str(job.status) != JobStatus.PENDING.value:
            return ScheduleDmResult(
                ok=False,
                status_code=409,
                payload={
                    "ok": False,
                    "error": "CANCEL_NOT_ALLOWED",
                    "error_code": "CANCEL_NOT_ALLOWED",
                    "message": f"Only PENDING scheduled DMs can be cancelled (status={job.status}).",
                    "status": job.status,
                },
            )
        job.status = JobStatus.CANCELLED.value
        job.updated_at = utc_now_naive()
        job.lease_until = None
        job.lease_owner = None
        db.commit()
        return ScheduleDmResult(
            ok=True,
            status_code=200,
            payload={
                "ok": True,
                "job_id": int(job.id),
                "status": JobStatus.CANCELLED.value,
                "status_label": "Cancelled",
            },
        )

    def list_jobs(
        self,
        db: Session,
        *,
        limit: int = 20,
        account_id: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        lim = max(1, min(int(limit or 20), 50))
        q = db.query(ScheduledJob).filter(ScheduledJob.type == MessageType.DM.value)
        if account_id is not None:
            q = q.filter(ScheduledJob.account_id == int(account_id))
        jobs = q.order_by(ScheduledJob.run_at.desc(), ScheduledJob.id.desc()).limit(lim).all()
        rows: list[dict[str, Any]] = []
        for job in jobs:
            tz = (job.schedule_timezone or "").strip() or DEFAULT_OWNER_TIMEZONE
            try:
                fields = owner_schedule_fields(job.run_at, tz)
            except Exception:
                fields = {
                    "scheduled_at_utc": job.run_at.isoformat() + "Z" if job.run_at else None,
                    "scheduled_at_local": None,
                    "timezone": tz,
                }
            rows.append(
                {
                    "job_id": int(job.id),
                    "account_id": int(job.account_id),
                    "peer": job.peer_id,
                    "peer_type": job.peer_type,
                    "status": job.status,
                    "status_label": _owner_status_label(job.status),
                    "scheduled_at_utc": fields.get("scheduled_at_utc"),
                    "scheduled_at_local": fields.get("scheduled_at_local"),
                    "timezone": fields.get("timezone") or tz,
                    "message_preview": (job.message_body or "")[:120],
                    "idempotency_key": scheduled_dm_idempotency_key(int(job.id)),
                }
            )
        return rows


def _owner_status_label(raw: Optional[str]) -> str:
    key = (raw or "").strip().upper()
    return {
        "PENDING": "Upcoming",
        "RUNNING": "Sending",
        "SENT": "Sent",
        "FAILED": "Failed",
        "CANCELLED": "Cancelled",
        "UNCERTAIN": "Uncertain",
        "SKIPPED": "Skipped",
    }.get(key, key or "Unknown")
