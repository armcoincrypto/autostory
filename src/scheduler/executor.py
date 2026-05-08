"""
Scheduler job executor - sends messages via Telethon
"""
import asyncio
import random
from datetime import datetime
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
    MessageTemplate, AccountTargetBinding, DeliveryStatus, JobStatus,
    SCHEDULED_JOB_OPERATOR_SEND_TEST_MARKER,
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
            job.updated_at = datetime.utcnow()
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


async def execute_job(job_id: int, *, is_send_test: bool = False) -> bool:
    """Execute a single scheduled job. Returns True if sent successfully."""
    effective_send_test = bool(is_send_test)
    with get_db_context() as db:
        job = db.query(ScheduledJob).filter(ScheduledJob.id == job_id).first()
        if not job:
            return False
        # Runnable jobs: queued (PENDING) or claimed by this worker loop (RUNNING).
        if str(job.status) not in (JobStatus.PENDING.value, JobStatus.RUNNING.value):
            return False
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
            job.updated_at = datetime.utcnow()
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

        if str(job.last_error or "").strip() == SCHEDULED_JOB_OPERATOR_SEND_TEST_MARKER:
            effective_send_test = True
            job.last_error = None
            job.updated_at = datetime.utcnow()

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

    # Get template body
    template_body = _get_template_body(job, account, target, binding)
    if not template_body:
        _mark_job_failed(job_id, "No template found")
        return False

    if _reconcile_job_if_sent_delivery_exists(job_id):
        return True

    account_name = account.first_name or account.username or account.phone_number
    rendered = render_template(
        template_body,
        account_name=account_name,
        chat_title=target.title,
    )

    if effective_send_test:
        mark_account_active(int(account.id))
        logger.info("send_test_started", job_id=job_id, account_id=int(account.id))

    # Get client and send (add_account resolves file vs string session and verifies auth)
    try:
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

        retryable_post = (
            ChatWriteForbiddenError,
            UserBannedInChannelError,
            SlowModeWaitError,
        )

        try:
            for attempt in (0, 1):
                try:
                    msg = await wrapper.execute(
                        wrapper.client.send_message,
                        entity,
                        rendered,
                    )
                    _mark_job_sent(job_id, msg.id, rendered)
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
                    _mark_job_failed(job_id, f"{code}: {msg}", error_code=code)
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
            _mark_job_failed(job_id, f"{code}: {msg}", error_code=code)
            raise
        except ValueError as e:
            err = str(e)
            hint = (
                "Entity resolution failed (wrong cached id or bad link). "
                "Use Advanced → Targets → Clear cached chat id, or fix username/invite."
            )
            if "PeerUser" in err or "entity" in err.lower():
                _mark_job_failed(job_id, f"{hint} ({err})", error_code="EntityResolution")
            else:
                _mark_job_failed(job_id, f"{hint} ({err})", error_code="ValueError")
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
            _mark_job_failed(job_id, f"{code}: {msg}", error_code=code)
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
            _mark_job_failed(job_id, f"{code}: {msg}", error_code=code)
            return False
        finally:
            try:
                await client_manager.remove_account(int(account.id))
            except Exception:
                pass
    finally:
        if effective_send_test:
            unmark_account_active(int(account.id))


def _get_template_body(job, account, target, binding) -> Optional[str]:
    """Resolve best template: binding > target > account > global"""
    import random
    with get_db_context() as db:
        for scope_id, scope in [(binding.id, "BINDING"), (target.id, "TARGET"), (account.id, "ACCOUNT"), (None, "GLOBAL")]:
            q = db.query(MessageTemplate).filter(
                MessageTemplate.type == job.type,
                MessageTemplate.scope == scope,
                MessageTemplate.is_active == True
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
                    MessageTemplate.binding_id.is_(None)
                )
            templates = q.order_by(MessageTemplate.weight.desc()).all()
            if templates:
                return random.choice(templates).body
    return None


def _mark_job_sent(job_id: int, tg_message_id: int, rendered: str) -> None:
    with get_db_context() as db:
        job = db.query(ScheduledJob).filter(ScheduledJob.id == job_id).first()
        if not job:
            return
        prev_lo, prev_lu = job.lease_owner, job.lease_until
        job.status = JobStatus.SENT.value
        job.updated_at = datetime.utcnow()
        job.lease_until = None
        job.lease_owner = None
        _maybe_log_lease_released(job_id, prev_lo, prev_lu, reason="terminal_sent")
        d = MessageDelivery(
            job_id=job_id,
            account_id=job.account_id,
            target_id=job.target_id,
            type=job.type,
            status=DeliveryStatus.SENT,
            sent_at=datetime.utcnow(),
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
        job.updated_at = datetime.utcnow()
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
            job.updated_at = datetime.utcnow()
            job.lease_until = None
            job.lease_owner = None
            _maybe_log_lease_released(job_id, prev_lo, prev_lu, reason="terminal_skipped")
