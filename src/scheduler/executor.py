# P9.38 draft restore — source-backed from Cursor snapshots (not byte-matched to archive .pyc).
# Do not restart scheduler until import probe + tests pass and operator approves.

"""
Scheduler job executor - sends messages via Telethon
"""
import asyncio
import os
import random
import uuid
from datetime import datetime, timedelta

from src.core.datetime_utc import utc_now_naive
from typing import Any, Optional, Tuple

import structlog
from sqlalchemy import func


class _NeverRaisedTelethonError(Exception):
    """
    Placeholder for Telethon RPC error classes missing in older ``telethon`` wheels.

    Telegram will never raise this type; it is only used so ``except (..., X, ...)``
    tuples remain valid at import time without widening catches to ``Exception``.
    """


# Version-safe Telethon error symbols (import-time failures here took down Gunicorn).
_TELETHON_ERROR_NAMES = (
    "ChannelPrivateError",
    "ChatGuestSendForbiddenError",
    "ChatRestrictedError",
    "ChatSendPlainForbiddenError",
    "ChatWriteForbiddenError",
    "FloodWaitError",
    "PeerFloodError",
    "SlowModeWaitError",
    "UserBannedInChannelError",
    "UserRestrictedError",
)

try:
    import telethon.errors as _telethon_errors_mod
except ImportError:  # telethon not installed (unit stubs / minimal env)
    _telethon_errors_mod = None  # type: ignore[assignment]

_g_exec = globals()
_candidate: Optional[type[BaseException]] = None
for _err_name in _TELETHON_ERROR_NAMES:
    _cls: type[BaseException] = _NeverRaisedTelethonError
    if _telethon_errors_mod is not None:
        _candidate = getattr(_telethon_errors_mod, _err_name, None)
        if isinstance(_candidate, type) and issubclass(_candidate, BaseException):
            _cls = _candidate
    _g_exec[_err_name] = _cls

del _g_exec, _err_name, _cls, _candidate, _TELETHON_ERROR_NAMES
try:
    del _telethon_errors_mod
except NameError:
    pass

from src.core.database import get_db_context
from src.ai_agent.account_allowlist import account_id_excluded_from_scheduler_worker
from src.core.models import Account, AccountStatus
from src.core.scheduler_models import (
    ScheduledJob, MessageDelivery, ChatTarget,
    MessageTemplate, AccountTargetBinding, DeliveryStatus, JobStatus, MessageType,
    SCHEDULED_JOB_OPERATOR_SEND_TEST_MARKER,
    SCHEDULED_JOB_CAMPAIGN_PILOT_MARKER,
    SCHEDULED_JOB_P4C_CERTIFICATION_MARKER,
    SCHEDULED_JOB_P5A_CERTIFICATION_MARKER,
)
from src.clients.manager import client_manager
from src.core.account_runtime_state import mark_account_active, unmark_account_active
from src.clients.session_resolve import human_message_for_code
from src.clients.target_health import (
    classify_target,
    HEALTH_INVALID,
    HEALTH_NEEDS_REPAIR,
    entity_probe_chain,
    is_health_allowed_for_send,
    merged_target_health_row,
)
from .renderer import render_template

logger = structlog.get_logger(__name__)


def _maybe_log_lease_released(
    job_id: int,
    prev_owner: Optional[str],
    prev_until: Optional[datetime],
    *,
    reason: str,
) -> None:
    if prev_owner is None and prev_until is None:
        return
    logger.info(
        "scheduler_job_lease_released",
        job_id=int(job_id),
        lease_owner=prev_owner,
        lease_until=prev_until,
        reason=reason,
    )


def _map_error(exc: Exception) -> Tuple[str, str]:
    """Map Telethon exception to (error_code, short operator-facing message)."""
    if isinstance(exc, FloodWaitError):
        return "FloodWait", f"Telegram FloodWait — wait {exc.seconds}s before retrying"
    if isinstance(exc, PeerFloodError):
        return "PeerFlood", "Telegram peer flood / too many messages to this chat — slow down"
    if isinstance(exc, SlowModeWaitError):
        secs = getattr(exc, "seconds", None)
        if secs is None:
            return "SlowModeWait", "Group slow mode — wait before sending again"
        return "SlowModeWait", f"Group slow mode — wait {secs}s before sending again"
    if isinstance(exc, ChatGuestSendForbiddenError):
        return "ChatGuestSendForbidden", "Cannot post as guest / anonymous in this chat — join with full account or get post rights"
    if isinstance(exc, ChatWriteForbiddenError):
        return "ChatWriteForbidden", "CHAT_WRITE_FORBIDDEN — no post permission (not the same as a channel ban)"
    if isinstance(exc, ChatSendPlainForbiddenError):
        return "ChatSendPlainForbidden", "This chat forbids plain-text messages for your role (try media or check group settings)"
    if isinstance(exc, UserBannedInChannelError):
        return (
            "UserBannedInChannel",
            "Telegram USER_BANNED_IN_CHANNEL — often a false positive right after join or during "
            "spam/slowmode windows. If you can post manually in the app, wait 30–60s and retry; "
            "otherwise the account may actually be removed from the group.",
        )
    if isinstance(exc, UserRestrictedError):
        return "UserRestricted", "Your Telegram account is restricted (spam/scam) — reduce activity and check Telegram settings"
    if isinstance(exc, ChatRestrictedError):
        return "ChatRestricted", "This chat is restricted for your account (try again later)"
    if isinstance(exc, ChannelPrivateError):
        return "ChannelPrivate", "Channel is private or not accessible with this session"
    return type(exc).__name__, str(exc)


async def _resolve_send_entity(client: Any, target: Any) -> Any:
    """
    Same resolution policy as membership/join: prefer username / invite over
    a bare cached ``tg_id`` to avoid ``PeerUser`` mismatches.
    """
    chain = entity_probe_chain(target)
    if not chain:
        return None
    last_exc: Optional[Exception] = None
    for ref in chain:
        try:
            return await client.get_entity(ref)
        except ChannelPrivateError:
            continue
        except Exception as e:
            last_exc = e
            continue
    if last_exc:
        raise last_exc
    return None


def _reconcile_job_if_sent_delivery_exists(job_id: int) -> bool:
    """
    If a SENT delivery row already exists for this job but the job is still PENDING
    or RUNNING, reconcile status and skip a second Telegram send (idempotency guard).

    FAILED / SKIPPED delivery rows do not trigger reconciliation.
    """
    _st = func.lower(func.trim(MessageDelivery.status))
    with get_db_context() as db:
        row = (
            db.query(MessageDelivery)
            .filter(MessageDelivery.job_id == int(job_id), _st == "sent")
            .order_by(MessageDelivery.id.desc())
            .first()
        )
        if not row:
            return False
        job = db.query(ScheduledJob).filter(ScheduledJob.id == int(job_id)).first()
        if job and str(job.status) in (JobStatus.PENDING.value, JobStatus.RUNNING.value):
            job.status = JobStatus.SENT.value
            job.updated_at = utc_now_naive()
            job.lease_until = None
            job.lease_owner = None
        logger.info(
            "scheduler_idempotent_sent_skip",
            job_id=int(job_id),
            delivery_id=int(row.id),
            tg_message_id=int(row.tg_message_id) if row.tg_message_id is not None else None,
        )
        return True


def _should_flip_can_post_on_send_failure(exc: Exception) -> bool:
    """Do not disable binding for clearly temporary / rate-limit / ambiguous ban signals."""
    if isinstance(
        exc,
        (
            FloodWaitError,
            PeerFloodError,
            SlowModeWaitError,
        ),
    ):
        return False
    # USER_BANNED_IN_CHANNEL is frequently wrong right after join; keep binding unless
    # operator confirms — merged health still reflects the failed delivery row.
    if isinstance(exc, UserBannedInChannelError):
        return False
    return True


def _delivery_status_lc():
    return func.lower(func.trim(MessageDelivery.status))


def _preflight_delivery_intent(job_id: int) -> Optional[str]:
    """
    Reconcile in-flight delivery rows before a new send attempt.

    Returns:
        ``uncertain_block`` — do not send (UNCERTAIN row exists).
        ``already_sending`` — another worker holds a valid lease for SENDING intent.
        ``None`` — safe to create a new SENDING row (stale SENDING rows are FAILED).
    """
    _st = _delivery_status_lc()
    with get_db_context() as db:
        job = db.query(ScheduledJob).filter(ScheduledJob.id == int(job_id)).first()
        if not job:
            return None
        uncertain = (
            db.query(MessageDelivery)
            .filter(MessageDelivery.job_id == int(job_id), _st == "uncertain")
            .first()
        )
        if uncertain is not None:
            return "uncertain_block"

        sending_rows = (
            db.query(MessageDelivery)
            .filter(MessageDelivery.job_id == int(job_id), _st == "sending")
            .order_by(MessageDelivery.id.asc())
            .all()
        )
        if not sending_rows:
            return None
        if len(sending_rows) > 1:
            for extra in sending_rows[:-1]:
                extra.status = DeliveryStatus.FAILED.value
                extra.error_code = "DUPLICATE_SENDING"
                extra.error_message = "Superseded send-intent row"
        latest = sending_rows[-1]
        now = utc_now_naive()
        lu = job.lease_until
        if str(job.status) == JobStatus.RUNNING.value and lu is not None and lu > now:
            return "already_sending"
        latest.status = DeliveryStatus.FAILED.value
        latest.error_code = "STALE_SENDING"
        latest.error_message = (
            "Stale SENDING intent (lease expired or job not RUNNING) — cleared before retry"
        )
    return None


def _fail_job_for_uncertain_block(job_id: int) -> None:
    with get_db_context() as db:
        job = db.query(ScheduledJob).filter(ScheduledJob.id == int(job_id)).first()
        if not job:
            return
        prev_lo, prev_lu = job.lease_owner, job.lease_until
        job.status = JobStatus.FAILED.value
        job.attempts = (job.attempts or 0) + 1
        job.last_error = "UNCERTAIN prior delivery — operator review required"
        job.updated_at = utc_now_naive()
        job.lease_until = None
        job.lease_owner = None
        _maybe_log_lease_released(job_id, prev_lo, prev_lu, reason="uncertain_block")


def _create_sending_delivery(
    job_id: int,
    account_id: int,
    target_id: int,
    type_: str,
    rendered: str,
) -> int:
    key = uuid.uuid4().hex[:32]
    with get_db_context() as db:
        now = utc_now_naive()
        d = MessageDelivery(
            job_id=int(job_id),
            account_id=int(account_id),
            target_id=int(target_id),
            type=type_,
            status=DeliveryStatus.SENDING.value,
            rendered_body=rendered[:500] if rendered else None,
            attempt_started_at=now,
            idempotency_key=key,
        )
        db.add(d)
        db.flush()
        did = int(d.id)
    logger.info(
        "scheduler_delivery_sending_created",
        job_id=int(job_id),
        delivery_id=did,
        idempotency_key=key,
    )
    return did


def _mark_delivery_uncertain(
    delivery_id: int,
    job_id: int,
    detail: str,
    *,
    error_code: str = "AMBIGUOUS_RPC",
) -> None:
    with get_db_context() as db:
        d = db.query(MessageDelivery).filter(MessageDelivery.id == int(delivery_id)).first()
        if d:
            d.status = DeliveryStatus.UNCERTAIN.value
            d.error_code = error_code
            d.error_message = detail[:2000] if detail else None
    logger.info(
        "scheduler_delivery_uncertain",
        job_id=int(job_id),
        delivery_id=int(delivery_id),
        error_code=error_code,
    )


def _finalize_delivery_sent(
    delivery_id: int,
    job_id: int,
    tg_message_id: int,
    rendered: str,
) -> None:
    with get_db_context() as db:
        d = db.query(MessageDelivery).filter(MessageDelivery.id == int(delivery_id)).first()
        if d:
            d.status = DeliveryStatus.SENT.value
            d.tg_message_id = tg_message_id
            d.sent_at = utc_now_naive()
            if not d.rendered_body and rendered:
                d.rendered_body = rendered[:500]
        job = db.query(ScheduledJob).filter(ScheduledJob.id == int(job_id)).first()
        if job:
            prev_lo, prev_lu = job.lease_owner, job.lease_until
            job.status = JobStatus.SENT.value
            job.updated_at = utc_now_naive()
            job.lease_until = None
            job.lease_owner = None
            _maybe_log_lease_released(job_id, prev_lo, prev_lu, reason="terminal_sent")
    logger.info(
        "scheduler_delivery_sent_finalized",
        job_id=int(job_id),
        delivery_id=int(delivery_id),
        tg_message_id=int(tg_message_id),
    )


def _finalize_delivery_failed(
    delivery_id: int,
    job_id: int,
    error: str,
    *,
    error_code: Optional[str] = None,
) -> None:
    with get_db_context() as db:
        d = db.query(MessageDelivery).filter(MessageDelivery.id == int(delivery_id)).first()
        if d:
            d.status = DeliveryStatus.FAILED.value
            d.error_code = error_code
            d.error_message = error[:2000] if error else None
        job = db.query(ScheduledJob).filter(ScheduledJob.id == int(job_id)).first()
        if job:
            prev_lo, prev_lu = job.lease_owner, job.lease_until
            job.status = JobStatus.FAILED.value
            job.attempts = (job.attempts or 0) + 1
            job.last_error = error
            job.updated_at = utc_now_naive()
            job.lease_until = None
            job.lease_owner = None
            _maybe_log_lease_released(job_id, prev_lo, prev_lu, reason="terminal_failed")


async def _execute_scheduled_dm_job(job_id: int) -> bool:
    """Wave 10: due DM job → OwnerDirectMessageService.send_now (stable idempotency)."""
    from src.messaging.owner_dm_service import OwnerDirectMessageService
    from src.messaging.scheduled_dm_service import scheduled_dm_idempotency_key

    with get_db_context() as db:
        job = db.query(ScheduledJob).filter(ScheduledJob.id == int(job_id)).first()
        if not job:
            return False
        if str(job.status) not in (JobStatus.PENDING.value, JobStatus.RUNNING.value):
            return False
        if str(job.type).upper() != MessageType.DM.value:
            return False

        account_id = int(job.account_id)
        peer_id = (job.peer_id or "").strip()
        peer_type = (job.peer_type or "private").strip().lower()
        text = job.message_body if job.message_body is not None else ""
        idem = scheduled_dm_idempotency_key(int(job.id))
        lease_owner = job.lease_owner

        if not peer_id or not str(text).strip():
            prev_lo, prev_lu = job.lease_owner, job.lease_until
            job.status = JobStatus.FAILED.value
            job.attempts = (job.attempts or 0) + 1
            job.last_error = "DM job missing peer_id or message_body"
            job.updated_at = utc_now_naive()
            job.lease_until = None
            job.lease_owner = None
            _maybe_log_lease_released(job_id, prev_lo, prev_lu, reason="dm_invalid_payload")
            return False

        # Persist RUNNING if still PENDING (claim normally already set RUNNING).
        if str(job.status) == JobStatus.PENDING.value:
            job.status = JobStatus.RUNNING.value
            job.updated_at = utc_now_naive()
            db.commit()

    svc = OwnerDirectMessageService()
    with get_db_context() as db:
        result = await svc.send_now(
            db,
            account_id=account_id,
            peer_id=peer_id,
            text=str(text),
            idempotency_key=idem,
            peer_type=peer_type,
            claim_owner=lease_owner or f"scheduler-dm-{job_id}",
        )

    status = str(result.get("status") or "").upper()
    err_code = result.get("error_code")
    err_msg = result.get("error_message") or err_code or "DM send failed"
    replay = bool(result.get("replay"))

    with get_db_context() as db:
        job = db.query(ScheduledJob).filter(ScheduledJob.id == int(job_id)).first()
        if not job:
            return False
        prev_lo, prev_lu = job.lease_owner, job.lease_until
        now = utc_now_naive()
        job.attempts = (job.attempts or 0) + (0 if replay and status == "SENT" else 1)
        job.updated_at = now
        job.lease_until = None
        job.lease_owner = None

        if status == "SENT":
            job.status = JobStatus.SENT.value
            job.last_error = None
            _maybe_log_lease_released(job_id, prev_lo, prev_lu, reason="dm_sent")
            logger.info(
                "scheduled_dm_job_sent",
                job_id=int(job_id),
                account_id=account_id,
                telegram_message_id=result.get("telegram_message_id"),
                replay=replay,
                idempotency_key=idem,
            )
            return True

        if status == "UNCERTAIN":
            # Do NOT mint a new idempotency key. Surface Uncertain for operator review.
            job.status = JobStatus.UNCERTAIN.value
            job.last_error = f"UNCERTAIN: {err_msg}"[:2000]
            _maybe_log_lease_released(job_id, prev_lo, prev_lu, reason="dm_uncertain")
            logger.warning(
                "scheduled_dm_job_uncertain",
                job_id=int(job_id),
                account_id=account_id,
                idempotency_key=idem,
                error_code=err_code,
            )
            return False

        # Eligibility / rate / kill-switch / transport failures — safe terminal fail.
        job.status = JobStatus.FAILED.value
        job.last_error = f"{err_code or 'FAILED'}: {err_msg}"[:2000]
        _maybe_log_lease_released(job_id, prev_lo, prev_lu, reason="dm_failed")
        logger.info(
            "scheduled_dm_job_failed",
            job_id=int(job_id),
            account_id=account_id,
            error_code=err_code,
            replay=replay,
            idempotency_key=idem,
        )
        return False


async def execute_job(job_id: int, *, is_send_test: bool = False) -> bool:
    """Execute a single scheduled job. Returns True if sent successfully."""
    effective_send_test = bool(is_send_test)
    preserved_job_marker = ""
    with get_db_context() as db:
        job = db.query(ScheduledJob).filter(ScheduledJob.id == job_id).first()
        if not job:
            return False
        # Runnable jobs: queued (PENDING) or claimed by this worker loop (RUNNING).
        if str(job.status) not in (JobStatus.PENDING.value, JobStatus.RUNNING.value):
            return False
        # Wave 10 — scheduled private DM (no chat_targets / binding path).
        if str(job.type).upper() == MessageType.DM.value:
            return await _execute_scheduled_dm_job(int(job_id))

        account = db.query(Account).filter(Account.id == job.account_id).first()
        target = db.query(ChatTarget).filter(ChatTarget.id == job.target_id).first()
        binding = db.query(AccountTargetBinding).filter(
            AccountTargetBinding.account_id == job.account_id,
            AccountTargetBinding.target_id == job.target_id
        ).first()

        if not account or not target or not binding:
            reason = "missing_account" if not account else ("missing_target" if not target else "missing_binding")
            # Disable the job so it is idempotent and won't spam logs every tick.
            prev_lo, prev_lu = job.lease_owner, job.lease_until
            job.status = JobStatus.FAILED.value
            job.attempts = (job.attempts or 0) + 1
            job.last_error = reason
            job.updated_at = utc_now_naive()
            job.lease_until = None
            job.lease_owner = None
            _maybe_log_lease_released(job_id, prev_lo, prev_lu, reason="terminal_missing_binding")
            logger.warning(
                "scheduler_job_disabled_missing_binding",
                job_id=job_id,
                reason=reason,
                account_id=getattr(job, "account_id", None),
                target_id=getattr(job, "target_id", None),
            )
            return False
        if not binding.can_post:
            logger.info("Binding can_post=False, skipping", job_id=job_id)
            _mark_job_skipped(job_id, "Binding disabled")
            return False
        th = classify_target(target)
        if th.get("health") == HEALTH_INVALID:
            logger.info("Target failed health check, skipping", job_id=job_id, reason=th.get("reason"))
            _mark_job_skipped(job_id, th.get("reason") or "Invalid target")
            return False
        if th.get("health") == HEALTH_NEEDS_REPAIR:
            logger.info(
                "Target needs repair (no username/invite), skipping",
                job_id=job_id,
                reason=th.get("reason"),
            )
            _mark_job_skipped(job_id, th.get("reason") or "Target needs repair")
            return False
        mh = merged_target_health_row(db, target, int(job.account_id))
        if not is_health_allowed_for_send(mh["health"]):
            logger.info(
                "Target not sendable (operational health), skipping",
                job_id=job_id,
                health=mh.get("health"),
                reason=mh.get("health_reason"),
            )
            _mark_job_skipped(
                job_id,
                mh.get("health_reason") or f"Target not sendable ({mh.get('health')})",
            )
            return False
        if account.status != AccountStatus.ACTIVE:
            logger.info("Account not active, skipping", job_id=job_id, status=account.status)
            _mark_job_skipped(job_id, "Account not active")
            return False

        if account_id_excluded_from_scheduler_worker(db, int(account.id)):
            logger.info(
                "scheduler_job_skipped_ai_reserved_account",
                job_id=job_id,
                account_id=int(account.id),
            )
            _mark_job_skipped(job_id, "account_reserved_for_ai_agent")
            return False

        from src.scheduler.runtime_preflight import classify_scheduler_runtime_gate
        from src.clients.session_resolve import ERR_LEGACY_SQLITE_SESSION_FORMAT

        job_marker = str(job.last_error or "").strip()
        preserved_job_marker = job_marker
        is_operator_send_test_job = job_marker in (
            SCHEDULED_JOB_OPERATOR_SEND_TEST_MARKER,
            SCHEDULED_JOB_CAMPAIGN_PILOT_MARKER,
            SCHEDULED_JOB_P4C_CERTIFICATION_MARKER,
            SCHEDULED_JOB_P5A_CERTIFICATION_MARKER,
        )
        gate = classify_scheduler_runtime_gate(db, account)
        if (
            is_operator_send_test_job
            and gate.get("action") == "defer_temp_lock"
            and gate.get("primary_blocker") == ERR_LEGACY_SQLITE_SESSION_FORMAT
        ):
            logger.info(
                "p9_40_operator_send_test_runtime_gate_override",
                job_id=int(job_id),
                account_id=int(account.id),
                prior_blockers=gate.get("temporary_blockers"),
            )
            gate = {
                **gate,
                "action": "proceed",
                "primary_blocker": None,
                "primary_label": "Operator send test (controlled)",
                "temporary_blockers": [],
            }
        logger.info(
            "scheduler_account_selected",
            job_id=int(job_id),
            account_id=int(account.id),
            gate_action=gate.get("action"),
            lifecycle_state=gate.get("lifecycle_state"),
            primary_blocker=gate.get("primary_blocker"),
        )
        if gate.get("action") == "defer_temp_lock":
            prev_lo, prev_lu = job.lease_owner, job.lease_until
            now = utc_now_naive()
            defer_sec = int(os.environ.get("SCHEDULER_TEMP_LOCK_DEFER_SEC", "90"))
            job.run_at = max(job.run_at or now, now) + timedelta(seconds=max(10, defer_sec))
            job.status = JobStatus.PENDING.value
            job.lease_until = None
            job.lease_owner = None
            job.last_error = "TEMP_LOCK_DEFERRED"
            job.updated_at = now
            _maybe_log_lease_released(job_id, prev_lo, prev_lu, reason="defer_temp_lock")
            logger.warning(
                "scheduler_account_temp_busy",
                job_id=int(job_id),
                account_id=int(account.id),
                temporary_blockers=gate.get("temporary_blockers"),
                primary_blocker=gate.get("primary_blocker"),
            )
            try:
                from src.core.session_lock import inspect_session_lock

                ls = inspect_session_lock(int(account.id))
                logger.info(
                    "scheduler_lock_owner",
                    job_id=int(job_id),
                    account_id=int(account.id),
                    lock_held=ls.held,
                    subsystem=ls.subsystem,
                    operation=ls.operation,
                    meta_pid=ls.meta_pid,
                    stale=ls.stale,
                )
            except Exception as exc:
                logger.warning("scheduler_lock_owner_inspect_failed", job_id=job_id, error=str(exc))
            logger.info(
                "scheduler_job_deferred_temp_lock",
                job_id=int(job_id),
                account_id=int(account.id),
                defer_sec=defer_sec,
            )
            return False
        if gate.get("action") == "skip_permanent":
            logger.warning(
                "scheduler_job_blocked_no_eligible_accounts",
                job_id=int(job_id),
                account_id=int(account.id),
                primary_blocker=gate.get("primary_blocker"),
                permanent_blockers=gate.get("permanent_blockers"),
                lifecycle_state=gate.get("lifecycle_state"),
            )
            reason = f"scheduler_ineligible:{gate.get('primary_blocker') or 'unknown'}"
            _mark_job_skipped(job_id, reason[:500])
            return False

        if job_marker in (
            SCHEDULED_JOB_OPERATOR_SEND_TEST_MARKER,
            SCHEDULED_JOB_CAMPAIGN_PILOT_MARKER,
            SCHEDULED_JOB_P4C_CERTIFICATION_MARKER,
            SCHEDULED_JOB_P5A_CERTIFICATION_MARKER,
        ):
            effective_send_test = True
            job.last_error = None
            job.updated_at = utc_now_naive()

        if not effective_send_test:
            from src.scheduler.pacing import get_send_pacing_decision, defer_scheduled_job_for_pacing

            pace = get_send_pacing_decision(
                db,
                int(job.account_id),
                int(job.target_id),
                is_test=False,
                binding_created_at=getattr(binding, "created_at", None),
            )
            if not pace.get("allowed"):
                nxt = pace.get("next_allowed_at_dt")
                if nxt:
                    defer_scheduled_job_for_pacing(db, job_id, nxt)
                logger.info(
                    "pacing_deferred",
                    job_id=job_id,
                    account_id=job.account_id,
                    target_id=job.target_id,
                    retry_after_sec=pace.get("retry_after_sec"),
                    next_allowed_at=pace.get("next_allowed_at"),
                )
                return False

        # Get template body (must stay inside this session: job/account/target are not valid after close)
        template_body, template_err = _resolve_template_for_job(
            db, job, account, target, binding
        )
        if template_err:
            _mark_job_failed(job_id, template_err[:500])
            return False
        if not template_body:
            _mark_job_failed(job_id, "No template found")
            return False

        if job_marker == SCHEDULED_JOB_CAMPAIGN_PILOT_MARKER:
            from src.scheduler.campaign_governance import check_governed_campaign_send_allowed

            allowed, block_reason = check_governed_campaign_send_allowed(
                db,
                int(job.account_id),
                int(job.target_id),
                job_marker=job_marker,
            )
            if not allowed:
                reason = (block_reason or "campaign_execution_blocked")[:500]
                logger.warning(
                    "campaign_governance_send_blocked",
                    job_id=job_id,
                    account_id=int(job.account_id),
                    target_id=int(job.target_id),
                    reason=reason,
                )
                _mark_job_failed(job_id, reason)
                return False

        if _reconcile_job_if_sent_delivery_exists(job_id):
            return True

        account_name = account.first_name or account.username or account.phone_number
        rendered = render_template(
            template_body,
            account_name=account_name,
            chat_title=target.title,
        )
        job_type_str = str(job.type)
        # Eager-load fields used after this session closes (Telethon / wrapper path).
        _ = (
            account.session_string,
            getattr(account, "session_path", None),
            account.proxy_config,
            account.phone_number,
        )
        _ = (
            getattr(target, "username", None),
            getattr(target, "invite_link", None),
            getattr(target, "tg_id", None),
            target.title,
        )
        db.expunge(account)
        db.expunge(target)
        db.expunge(job)
        db.expunge(binding)

    is_p4c_certification_job = preserved_job_marker == SCHEDULED_JOB_P4C_CERTIFICATION_MARKER
    is_p5a_certification_job = preserved_job_marker == SCHEDULED_JOB_P5A_CERTIFICATION_MARKER

    if effective_send_test:
        mark_account_active(int(account.id))
        logger.info("send_test_started", job_id=job_id, account_id=int(account.id))

    # Get client and send (add_account resolves file vs string session and verifies auth)
    try:
        pre = _preflight_delivery_intent(job_id)
        if pre == "uncertain_block":
            logger.info("scheduler_delivery_uncertain_block", job_id=job_id)
            _fail_job_for_uncertain_block(job_id)
            return False
        if pre == "already_sending":
            logger.info("scheduler_delivery_already_sending", job_id=job_id)
            return False

        from src.core.execution_guard import ACTION_TELEGRAM_SEND, can_execute_action

        with get_db_context() as _guard_db:
            _job_row = _guard_db.query(ScheduledJob).filter(ScheduledJob.id == job_id).first()
            _guard_decision = can_execute_action(
                ACTION_TELEGRAM_SEND,
                account_id=int(account.id),
                target_id=int(getattr(_job_row, "target_id", None) or target.id),
                db=_guard_db,
                job_marker=preserved_job_marker or None,
                job_id=int(job_id),
            )
        if not _guard_decision.allowed:
            logger.warning(
                "scheduler_send_blocked_execution_guard",
                job_id=job_id,
                account_id=int(account.id),
                reason=_guard_decision.reason_code,
            )
            _mark_job_skipped(job_id, f"execution_guard:{_guard_decision.reason_code}"[:500])
            return False

        wrapper, fail_reason = await client_manager.add_account(account)
        if not wrapper:
            detail = human_message_for_code(fail_reason) if fail_reason else "Failed to get client"
            _mark_job_failed(job_id, detail)
            return False
        if not wrapper.is_connected:
            if not await wrapper.connect():
                _mark_job_failed(
                    job_id,
                    human_message_for_code("failed_connect") or "Failed to connect",
                )
                try:
                    await client_manager.remove_account(int(account.id))
                except Exception:
                    pass
                return False

        try:
            entity = await _resolve_send_entity(wrapper.client, target)
        except Exception as e:
            code, msg = _map_error(e)
            _mark_job_failed(job_id, f"{code}: {msg}", error_code=code)
            return False
        if not entity:
            _mark_job_failed(job_id, "Target has no tg_id/username/link", error_code="NoEntity")
            return False

        delivery_id = _create_sending_delivery(
            job_id,
            int(account.id),
            int(target.id),
            job_type_str,
            rendered,
        )

        timeout_sec = float(os.environ.get("SCHEDULER_SEND_RPC_TIMEOUT_SEC", "120"))

        retryable_post = (
            ChatWriteForbiddenError,
            UserBannedInChannelError,
            SlowModeWaitError,
        )
        max_send_attempts = 1 if (is_p4c_certification_job or is_p5a_certification_job) else 2

        try:
            for attempt in range(max_send_attempts):
                try:
                    async def _invoke_send():
                        return await wrapper.execute(
                            wrapper.client.send_message,
                            entity,
                            rendered,
                        )

                    msg = await asyncio.wait_for(_invoke_send(), timeout=timeout_sec)
                    _finalize_delivery_sent(delivery_id, job_id, int(msg.id), rendered)
                    if is_p4c_certification_job:
                        try:
                            from src.core.p4c_send_counter import record_p4c_live_send

                            record_p4c_live_send(
                                job_id=int(job_id),
                                account_id=int(account.id),
                                target_id=int(target.id),
                            )
                        except Exception:
                            logger.warning("p4c_send_counter_record_failed", job_id=job_id)
                    if is_p5a_certification_job:
                        try:
                            from src.core.p5a_send_counter import record_p5a_live_send
                            from src.core.p5a_authorization import load_manifest

                            manifest = load_manifest() or {}
                            record_p5a_live_send(
                                job_id=int(job_id),
                                account_id=int(account.id),
                                target_id=int(target.id),
                                authorization_id=str(manifest.get("authorization_id") or ""),
                                tg_message_id=int(msg.id),
                            )
                        except Exception:
                            logger.warning("p5a_send_counter_record_failed", job_id=job_id)
                    logger.info("Message sent", job_id=job_id, tg_msg_id=msg.id)
                    try:
                        from src.clients.readiness_store import mark_account_ready_after_success

                        with get_db_context() as db:
                            mark_account_ready_after_success(db, int(account.id), "scheduled_send_ok")
                    except Exception:
                        logger.warning(
                            "readiness_mark_ready_after_send_failed",
                            job_id=job_id,
                            account_id=getattr(account, "id", None),
                        )
                    return True
                except asyncio.TimeoutError:
                    _mark_delivery_uncertain(
                        delivery_id,
                        job_id,
                        f"send_message RPC timeout after {timeout_sec}s",
                        error_code="SEND_TIMEOUT",
                    )
                    return False
                except retryable_post as e:
                    if attempt == 0:
                        delay = 8.0 + random.random() * 4.0
                        logger.info(
                            "scheduled_send_retry_after_transient",
                            job_id=job_id,
                            delay_sec=delay,
                            err=type(e).__name__,
                        )
                        await asyncio.sleep(delay)
                        try:
                            entity = await _resolve_send_entity(wrapper.client, target)
                        except Exception:
                            pass
                        continue
                    code, msg = _map_error(e)
                    _finalize_delivery_failed(delivery_id, job_id, f"{code}: {msg}", error_code=code)
                    if _should_flip_can_post_on_send_failure(e):
                        with get_db_context() as db:
                            b = db.query(AccountTargetBinding).filter(
                                AccountTargetBinding.account_id == account.id,
                                AccountTargetBinding.target_id == target.id,
                            ).first()
                            if b:
                                b.can_post = False
                    return False
        except FloodWaitError as e:
            code, msg = _map_error(e)
            _finalize_delivery_failed(delivery_id, job_id, f"{code}: {msg}", error_code=code)
            raise
        except ValueError as e:
            err = str(e)
            hint = (
                "Entity resolution failed (wrong cached id or bad link). "
                "Use Advanced → Targets → Clear cached chat id, or fix username/invite."
            )
            if "PeerUser" in err or "entity" in err.lower():
                _finalize_delivery_failed(
                    delivery_id, job_id, f"{hint} ({err})", error_code="EntityResolution"
                )
            else:
                _finalize_delivery_failed(
                    delivery_id, job_id, f"{hint} ({err})", error_code="ValueError"
                )
            return False
        except (
            ChatWriteForbiddenError,
            ChannelPrivateError,
            UserBannedInChannelError,
            ChatGuestSendForbiddenError,
            ChatSendPlainForbiddenError,
            UserRestrictedError,
            ChatRestrictedError,
            SlowModeWaitError,
            PeerFloodError,
        ) as e:
            code, msg = _map_error(e)
            _finalize_delivery_failed(delivery_id, job_id, f"{code}: {msg}", error_code=code)
            if _should_flip_can_post_on_send_failure(e):
                with get_db_context() as db:
                    b = db.query(AccountTargetBinding).filter(
                        AccountTargetBinding.account_id == account.id,
                        AccountTargetBinding.target_id == target.id
                    ).first()
                    if b:
                        b.can_post = False
            return False
        except Exception as e:
            code, msg = _map_error(e)
            _mark_delivery_uncertain(
                delivery_id,
                job_id,
                f"{code}: {msg}",
                error_code="AMBIGUOUS_RPC",
            )
            return False
        finally:
            try:
                await client_manager.remove_account(int(account.id))
            except Exception:
                pass
    finally:
        if effective_send_test:
            unmark_account_active(int(account.id))


def _resolve_template_for_job(
    db,
    job,
    account,
    target,
    binding,
) -> tuple[Optional[str], Optional[str]]:
    """Return (body, error). Campaign pilot jobs fail closed on ambiguity."""
    from src.scheduler.campaign_pilot_template import (
        is_campaign_pilot_job,
        resolve_campaign_pilot_template_body,
    )

    if is_campaign_pilot_job(job):
        body, err = resolve_campaign_pilot_template_body(
            db, job, account, target, binding
        )
        return body, err
    return _get_template_body_legacy(db, job, account, target, binding), None


def _get_template_body_legacy(db, job, account, target, binding) -> Optional[str]:
    """Resolve best template: binding > target > account > global (random within scope)."""
    import random

    for scope_id, scope in [
        (binding.id, "BINDING"),
        (target.id, "TARGET"),
        (account.id, "ACCOUNT"),
        (None, "GLOBAL"),
    ]:
        q = db.query(MessageTemplate).filter(
            MessageTemplate.type == job.type,
            MessageTemplate.scope == scope,
            MessageTemplate.is_active == True,
        )
        if scope == "BINDING":
            q = q.filter(MessageTemplate.binding_id == scope_id)
        elif scope == "TARGET":
            q = q.filter(MessageTemplate.target_id == scope_id)
        elif scope == "ACCOUNT":
            q = q.filter(MessageTemplate.account_id == scope_id)
        elif scope == "GLOBAL":
            q = q.filter(
                MessageTemplate.account_id.is_(None),
                MessageTemplate.target_id.is_(None),
                MessageTemplate.binding_id.is_(None),
            )
        templates = q.order_by(MessageTemplate.weight.desc()).all()
        if templates:
            return random.choice(templates).body
    return None


def _get_template_body(job, account, target, binding) -> Optional[str]:
    """Backward-compatible wrapper (legacy callers / tests)."""
    with get_db_context() as db:
        body, _err = _resolve_template_for_job(db, job, account, target, binding)
    return body


def _mark_job_sent(job_id: int, tg_message_id: int, rendered: str) -> None:
    with get_db_context() as db:
        job = db.query(ScheduledJob).filter(ScheduledJob.id == job_id).first()
        if not job:
            return
        prev_lo, prev_lu = job.lease_owner, job.lease_until
        job.status = JobStatus.SENT.value
        job.updated_at = utc_now_naive()
        job.lease_until = None
        job.lease_owner = None
        _maybe_log_lease_released(job_id, prev_lo, prev_lu, reason="terminal_sent")
        d = MessageDelivery(
            job_id=job_id,
            account_id=job.account_id,
            target_id=job.target_id,
            type=job.type,
            status=DeliveryStatus.SENT,
            sent_at=utc_now_naive(),
            tg_message_id=tg_message_id,
            rendered_body=rendered[:500] if rendered else None,
        )
        db.add(d)


def _mark_job_failed(job_id: int, error: str, *, error_code: Optional[str] = None) -> None:
    with get_db_context() as db:
        job = db.query(ScheduledJob).filter(ScheduledJob.id == job_id).first()
        if not job:
            return
        prev_lo, prev_lu = job.lease_owner, job.lease_until
        job.status = JobStatus.FAILED.value
        job.attempts = (job.attempts or 0) + 1
        job.last_error = error
        job.updated_at = utc_now_naive()
        job.lease_until = None
        job.lease_owner = None
        _maybe_log_lease_released(job_id, prev_lo, prev_lu, reason="terminal_failed")
        d = MessageDelivery(
            job_id=job_id,
            account_id=job.account_id,
            target_id=job.target_id,
            type=job.type,
            status=DeliveryStatus.FAILED,
            error_code=error_code,
            error_message=error,
        )
        db.add(d)


def _mark_job_skipped(job_id: int, reason: str) -> None:
    with get_db_context() as db:
        job = db.query(ScheduledJob).filter(ScheduledJob.id == job_id).first()
        if job:
            prev_lo, prev_lu = job.lease_owner, job.lease_until
            job.status = JobStatus.SKIPPED.value
            job.last_error = reason
            job.updated_at = utc_now_naive()
            job.lease_until = None
            job.lease_owner = None
            _maybe_log_lease_released(job_id, prev_lo, prev_lu, reason="terminal_skipped")
