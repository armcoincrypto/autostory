"""Multi-account Messages orchestration (Wave Q).

Creates N independent ScheduledDirectMessageService jobs with spacing.
Never sends Telegram itself. Never joins. Readiness uses OwnerChatService.preview.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from src.core.models import Account
from src.core.scheduler_models import JobStatus, MessageType, ScheduledJob
from src.messaging.eligibility import evaluate_dm_account_eligibility
from src.messaging.owner_chat_service import OwnerChatService
from src.messaging.peer_labels import (
    account_bucket,
    owner_unavailable_label,
)
from src.messaging.scheduled_dm_service import ScheduledDirectMessageService
from src.scheduler.timezone import (
    DEFAULT_OWNER_TIMEZONE,
    SchedulerTimezoneError,
    parse_owner_local_datetime,
)


def _bulk_marker(bulk_key: str) -> str:
    return f"bulk:{bulk_key.strip()}"


def _find_bulk_job_for_account(
    db: Session,
    *,
    bulk_key: str,
    account_id: int,
    peer_id: str,
    message: str,
    run_at_utc_naive: Optional[datetime] = None,
) -> Optional[ScheduledJob]:
    """Locate an already-created job for this bulk request + account (no silent duplicates)."""
    marker = _bulk_marker(bulk_key)
    by_marker = (
        db.query(ScheduledJob)
        .filter(ScheduledJob.type == MessageType.DM.value)
        .filter(ScheduledJob.account_id == int(account_id))
        .filter(ScheduledJob.last_error == marker)
        .order_by(ScheduledJob.id.desc())
        .first()
    )
    if by_marker is not None:
        return by_marker
    # After execution/cancel, marker may be cleared — match durable fingerprint.
    q = (
        db.query(ScheduledJob)
        .filter(ScheduledJob.type == MessageType.DM.value)
        .filter(ScheduledJob.account_id == int(account_id))
        .filter(ScheduledJob.peer_id == str(peer_id))
        .filter(ScheduledJob.message_body == message)
        .filter(ScheduledJob.status != JobStatus.CANCELLED.value)
        .order_by(ScheduledJob.id.desc())
    )
    if run_at_utc_naive is not None:
        window = timedelta(seconds=2)
        q = q.filter(ScheduledJob.run_at >= run_at_utc_naive - window).filter(
            ScheduledJob.run_at <= run_at_utc_naive + window
        )
    return q.first()

ALLOWED_SPACING_SEC = frozenset({30, 60, 120, 300})
MATRIX_ACCOUNT_CAP = 40


@dataclass
class BulkScheduleResult:
    ok: bool
    status_code: int
    payload: dict[str, Any]


def _account_display(a: Account) -> str:
    return (
        (a.first_name and str(a.first_name).strip())
        or (a.username and f"@{a.username}")
        or f"Account #{a.id}"
    )


def map_readiness_status(
    *,
    eligible: bool,
    eligibility_code: Optional[str],
    preview: Optional[dict[str, Any]],
) -> tuple[str, str, bool]:
    """Return (status_key, owner_label, selectable_ready)."""
    if not eligible:
        code = (eligibility_code or "").strip().upper()
        if code in {"PROTECTED"}:
            return "protected", "Protected", False
        if code in {"RESERVED"}:
            return "reserved", "Reserved", False
        if code in {"DISABLED", "ACCOUNT_DISABLED"}:
            return "disabled", "Disabled", False
        if code in {"AUTH_FAILED", "AUTH_REQUIRED", "NEEDS_SESSION"}:
            return "needs_login", "Needs login", False
        return "unavailable", owner_unavailable_label(code), False

    if not preview or not preview.get("ok"):
        msg = (preview or {}).get("message") or "Temporarily unavailable"
        return "temp_unavailable", "Temporarily unavailable", False

    if preview.get("already_joined"):
        if preview.get("can_post") is False:
            return "cannot_post", "Cannot post", False
        return "ready", "Ready", True

    message = str(preview.get("message") or "").lower()
    if "waiting" in message and "admin" in message:
        return "waiting_approval", "Waiting for approval", False
    return "not_joined", "Not joined", False


def compute_spaced_slots(
    *,
    local_date: str,
    local_time: str,
    timezone_name: str,
    count: int,
    spacing_sec: int,
) -> list[dict[str, Any]]:
    """Return per-slot local_date/local_time/utc for N spaced jobs."""
    if int(spacing_sec) not in ALLOWED_SPACING_SEC:
        raise SchedulerTimezoneError(
            "INVALID_SPACING",
            f"spacing_sec must be one of {sorted(ALLOWED_SPACING_SEC)}.",
        )
    if count < 1:
        raise SchedulerTimezoneError("INVALID_COUNT", "At least one account is required.")
    instant = parse_owner_local_datetime(local_date, local_time, timezone_name)
    tz = ZoneInfo(instant.timezone_name)
    slots: list[dict[str, Any]] = []
    for i in range(int(count)):
        utc_naive = instant.utc_naive + timedelta(seconds=int(spacing_sec) * i)
        local = utc_naive.replace(tzinfo=timezone.utc).astimezone(tz)
        slots.append(
            {
                "index": i,
                "local_date": local.strftime("%Y-%m-%d"),
                "local_time": local.strftime("%H:%M:%S"),
                "scheduled_at_utc": utc_naive.isoformat() + "Z",
                "scheduled_at_local": f"{local.strftime('%d %b %Y, %H:%M')} {instant.timezone_name}",
            }
        )
    return slots


class MultiAccountScheduleOrchestrator:
    """Thin batch create/cancel over ScheduledDirectMessageService + chat preview."""

    def __init__(
        self,
        *,
        chat_service: Optional[OwnerChatService] = None,
        schedule_service: Optional[ScheduledDirectMessageService] = None,
        run_async=None,
    ) -> None:
        self._chat = chat_service or OwnerChatService()
        self._sched = schedule_service or ScheduledDirectMessageService()
        if run_async is None:
            from src.clients.telethon_runtime import run as telethon_run

            self._run_async = telethon_run
        else:
            self._run_async = run_async

    def build_matrix(
        self,
        db: Session,
        *,
        ref: str,
        account_ids: Optional[list[int]] = None,
        probe_telegram: bool = True,
    ) -> dict[str, Any]:
        raw = (ref or "").strip()
        if not raw:
            return {"ok": False, "error": "PEER_INVALID", "message": "Chat reference required."}

        q = db.query(Account)
        if account_ids:
            ids = [int(x) for x in account_ids][:MATRIX_ACCOUNT_CAP]
            q = q.filter(Account.id.in_(ids))
            accounts = q.all()
        else:
            # Prefer scanning eligible accounts first (cap).
            accounts = q.order_by(Account.id.asc()).all()

        rows_out: list[dict[str, Any]] = []
        title = None
        chat_type = None
        peer_id = None
        scanned = 0

        # Sort: evaluate eligibility first without Telegram for non-selected bulk.
        prepared: list[tuple[Account, Any]] = []
        for a in accounts:
            elig = evaluate_dm_account_eligibility(db, int(a.id))
            prepared.append((a, elig))

        if not account_ids:
            # Eligible first, then others; cap Telegram probes.
            prepared.sort(key=lambda x: (0 if x[1].eligible else 1, int(x[0].id)))

        telegram_budget = MATRIX_ACCOUNT_CAP
        for a, elig in prepared:
            if scanned >= MATRIX_ACCOUNT_CAP:
                break
            preview = None
            if probe_telegram and elig.eligible and telegram_budget > 0:
                try:
                    preview = self._run_async(self._chat.preview_async(int(a.id), raw))
                    telegram_budget -= 1
                    if preview and preview.get("title") and not title:
                        title = preview.get("title")
                        chat_type = preview.get("chat_type")
                        peer_id = preview.get("peer_id")
                except Exception:
                    preview = {"ok": False, "message": "Temporarily unavailable"}
            status_key, status_label, ready = map_readiness_status(
                eligible=bool(elig.eligible),
                eligibility_code=elig.code,
                preview=preview,
            )
            # Enrich chat metadata from any ready preview
            if ready and preview:
                title = title or preview.get("title")
                chat_type = chat_type or preview.get("chat_type")
                peer_id = peer_id or preview.get("peer_id")

            rows_out.append(
                {
                    "account_id": int(a.id),
                    "display_name": _account_display(a),
                    "eligible": bool(elig.eligible),
                    "eligibility_code": elig.code,
                    "bucket": account_bucket(bool(elig.eligible), elig.code),
                    "status": status_key,
                    "status_label": status_label,
                    "ready": ready,
                    "already_joined": bool((preview or {}).get("already_joined")),
                    "can_post": (preview or {}).get("can_post"),
                    "peer_id": (preview or {}).get("peer_id"),
                }
            )
            scanned += 1

        counts = {
            "ready": sum(1 for r in rows_out if r["status"] == "ready"),
            "not_joined": sum(1 for r in rows_out if r["status"] == "not_joined"),
            "waiting_approval": sum(1 for r in rows_out if r["status"] == "waiting_approval"),
            "cannot_post": sum(1 for r in rows_out if r["status"] == "cannot_post"),
            "unavailable": sum(
                1
                for r in rows_out
                if r["status"]
                in {
                    "protected",
                    "reserved",
                    "disabled",
                    "needs_login",
                    "unavailable",
                    "temp_unavailable",
                }
            ),
            "total": len(rows_out),
        }
        return {
            "ok": True,
            "ref": raw,
            "title": title,
            "chat_type": chat_type,
            "peer_id": peer_id,
            "accounts": rows_out,
            "counts": counts,
        }

    def preview_bulk(
        self,
        db: Session,
        *,
        ref: str,
        account_ids: list[int],
        message: str,
        local_date: str,
        local_time: str,
        timezone_name: Optional[str] = None,
        spacing_sec: int = 60,
    ) -> BulkScheduleResult:
        matrix = self.build_matrix(db, ref=ref, account_ids=account_ids, probe_telegram=True)
        if not matrix.get("ok"):
            return BulkScheduleResult(False, 400, matrix)

        by_id = {int(r["account_id"]): r for r in matrix["accounts"]}
        ready_ids: list[int] = []
        skipped: list[dict[str, Any]] = []
        for aid in account_ids:
            row = by_id.get(int(aid))
            if not row or not row.get("ready"):
                skipped.append(
                    {
                        "account_id": int(aid),
                        "status_label": (row or {}).get("status_label") or "Not ready",
                    }
                )
                continue
            ready_ids.append(int(aid))

        if not ready_ids:
            return BulkScheduleResult(
                False,
                400,
                {
                    "ok": False,
                    "error": "NO_READY_ACCOUNTS",
                    "message": "No Ready accounts selected. Only Ready accounts can be scheduled.",
                    "skipped": skipped,
                },
            )

        tz = (timezone_name or "").strip() or DEFAULT_OWNER_TIMEZONE
        try:
            slots = compute_spaced_slots(
                local_date=local_date,
                local_time=local_time,
                timezone_name=tz,
                count=len(ready_ids),
                spacing_sec=int(spacing_sec),
            )
        except SchedulerTimezoneError as e:
            return BulkScheduleResult(
                False,
                400,
                {
                    "ok": False,
                    "error": getattr(e, "code", None) or "INVALID_DATETIME",
                    "message": str(e),
                },
            )

        plan = []
        for aid, slot in zip(ready_ids, slots):
            row = by_id[aid]
            plan.append(
                {
                    "account_id": aid,
                    "display_name": row["display_name"],
                    "local_date": slot["local_date"],
                    "local_time": slot["local_time"],
                    "scheduled_at_local": slot["scheduled_at_local"],
                    "scheduled_at_utc": slot["scheduled_at_utc"],
                    "peer_id": row.get("peer_id") or matrix.get("peer_id"),
                }
            )

        return BulkScheduleResult(
            True,
            200,
            {
                "ok": True,
                "would_schedule": True,
                "chat_title": matrix.get("title") or ref,
                "chat_type": matrix.get("chat_type"),
                "peer_id": matrix.get("peer_id"),
                "ref": ref,
                "message_preview": (message or "")[:160],
                "timezone": tz,
                "spacing_sec": int(spacing_sec),
                "accounts": len(plan),
                "jobs_to_create": len(plan),
                "starts_local": plan[0]["scheduled_at_local"] if plan else None,
                "ends_local": plan[-1]["scheduled_at_local"] if plan else None,
                "plan": plan,
                "skipped": skipped,
                "transport_send_count": 0,
            },
        )

    def create_bulk(
        self,
        db: Session,
        *,
        ref: str,
        account_ids: list[int],
        message: str,
        local_date: str,
        local_time: str,
        timezone_name: Optional[str] = None,
        spacing_sec: int = 60,
        peer_type: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        dry_run_only: bool = False,
    ) -> BulkScheduleResult:
        preview = self.preview_bulk(
            db,
            ref=ref,
            account_ids=account_ids,
            message=message,
            local_date=local_date,
            local_time=local_time,
            timezone_name=timezone_name,
            spacing_sec=spacing_sec,
        )
        if not preview.ok:
            return preview
        if dry_run_only:
            return preview

        bulk_key = (idempotency_key or "").strip()
        payload = preview.payload
        chat_type = (peer_type or payload.get("chat_type") or "supergroup").strip().lower()
        created: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        reused = 0

        for item in payload.get("plan") or []:
            peer = (item.get("peer_id") or payload.get("peer_id") or "").strip()
            if not peer:
                failed.append(
                    {
                        "account_id": item["account_id"],
                        "error": "PEER_INVALID",
                        "message": "Missing peer id for Ready account.",
                    }
                )
                continue

            run_at_naive = None
            utc_s = (item.get("scheduled_at_utc") or "").rstrip("Z")
            if utc_s:
                try:
                    run_at_naive = datetime.fromisoformat(utc_s)
                except ValueError:
                    run_at_naive = None

            if bulk_key:
                existing = _find_bulk_job_for_account(
                    db,
                    bulk_key=bulk_key,
                    account_id=int(item["account_id"]),
                    peer_id=str(peer),
                    message=message,
                    run_at_utc_naive=run_at_naive,
                )
                if existing is not None:
                    reused += 1
                    created.append(
                        {
                            "account_id": item["account_id"],
                            "display_name": item.get("display_name"),
                            "job_id": int(existing.id),
                            "scheduled_at_local": item.get("scheduled_at_local"),
                            "reused": True,
                        }
                    )
                    continue

            result = self._sched.schedule(
                db,
                account_id=int(item["account_id"]),
                peer_id=str(peer),
                message=message,
                local_date=item["local_date"],
                local_time=item["local_time"],
                timezone=payload.get("timezone"),
                peer_type=chat_type,
                dry_run_only=False,
            )
            if result.ok and result.payload.get("job_id"):
                jid = int(result.payload["job_id"])
                if bulk_key:
                    job = db.query(ScheduledJob).filter(ScheduledJob.id == jid).first()
                    if job and str(job.status) == JobStatus.PENDING.value:
                        job.last_error = _bulk_marker(bulk_key)
                        db.commit()
                created.append(
                    {
                        "account_id": item["account_id"],
                        "display_name": item.get("display_name"),
                        "job_id": jid,
                        "scheduled_at_local": result.payload.get("scheduled_at_local"),
                        "reused": False,
                    }
                )
            else:
                failed.append(
                    {
                        "account_id": item["account_id"],
                        "error": result.payload.get("error")
                        or result.payload.get("error_code"),
                        "message": result.payload.get("message")
                        or "Could not create schedule.",
                    }
                )

        ok = len(created) > 0
        return BulkScheduleResult(
            ok,
            200 if ok else 400,
            {
                "ok": ok,
                "created": len(created),
                "failed": len(failed),
                "reused": reused,
                "replay": reused > 0 and reused == len(created) and not failed,
                "jobs": created,
                "errors": failed,
                "skipped": payload.get("skipped") or [],
                "chat_title": payload.get("chat_title"),
                "spacing_sec": payload.get("spacing_sec"),
                "preview": payload,
            },
        )

    def cancel_bulk(self, db: Session, *, job_ids: list[int]) -> BulkScheduleResult:
        cancelled: list[int] = []
        rejected: list[dict[str, Any]] = []
        for jid in job_ids:
            result = self._sched.cancel(db, job_id=int(jid))
            if result.ok:
                cancelled.append(int(jid))
            else:
                rejected.append(
                    {
                        "job_id": int(jid),
                        "error": result.payload.get("error_code")
                        or result.payload.get("error"),
                        "message": result.payload.get("message"),
                        "status": result.payload.get("status"),
                    }
                )
        return BulkScheduleResult(
            True,
            200,
            {
                "ok": True,
                "cancelled": len(cancelled),
                "rejected": len(rejected),
                "job_ids": cancelled,
                "errors": rejected,
            },
        )


def scheduled_ops_summary(db: Session, *, timezone_name: str = DEFAULT_OWNER_TIMEZONE) -> dict[str, Any]:
    """DB-only counts for Messages ops strip (no Telegram)."""
    tz = ZoneInfo(timezone_name or DEFAULT_OWNER_TIMEZONE)
    now_local = datetime.now(tz)
    start_local = datetime(now_local.year, now_local.month, now_local.day, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    start_utc = start_local.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = end_local.astimezone(timezone.utc).replace(tzinfo=None)

    q = db.query(ScheduledJob).filter(ScheduledJob.type == MessageType.DM.value)
    upcoming_today = (
        q.filter(ScheduledJob.status == JobStatus.PENDING.value)
        .filter(ScheduledJob.run_at >= start_utc)
        .filter(ScheduledJob.run_at < end_utc)
        .count()
    )
    sent_today = (
        db.query(ScheduledJob)
        .filter(ScheduledJob.type == MessageType.DM.value)
        .filter(ScheduledJob.status == JobStatus.SENT.value)
        .filter(ScheduledJob.run_at >= start_utc)
        .filter(ScheduledJob.run_at < end_utc)
        .count()
    )
    failed = (
        db.query(ScheduledJob)
        .filter(ScheduledJob.type == MessageType.DM.value)
        .filter(ScheduledJob.status == JobStatus.FAILED.value)
        .count()
    )
    uncertain = (
        db.query(ScheduledJob)
        .filter(ScheduledJob.type == MessageType.DM.value)
        .filter(ScheduledJob.status == JobStatus.UNCERTAIN.value)
        .count()
    )
    return {
        "ok": True,
        "timezone": timezone_name or DEFAULT_OWNER_TIMEZONE,
        "upcoming_today": int(upcoming_today),
        "sent_today": int(sent_today),
        "failed": int(failed),
        "uncertain": int(uncertain),
    }
