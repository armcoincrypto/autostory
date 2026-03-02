"""
Scheduler job executor - sends messages via Telethon
"""
from datetime import datetime
from typing import Optional, Tuple

from telethon.errors import (
    FloodWaitError,
    ChatWriteForbiddenError,
    ChannelPrivateError,
    UserBannedInChannelError,
)
import structlog

from src.core.database import get_db_context
from src.core.models import Account, AccountStatus
from src.core.scheduler_models import (
    ScheduledJob, MessageDelivery, ChatTarget,
    MessageTemplate, AccountTargetBinding, DeliveryStatus, JobStatus
)
from src.clients.manager import client_manager
from .renderer import render_template

logger = structlog.get_logger(__name__)


def _map_error(exc: Exception) -> Tuple[str, str]:
    """Map Telethon exception to error_code, error_message"""
    if isinstance(exc, FloodWaitError):
        return "FloodWait", f"Wait {exc.seconds}s"
    if isinstance(exc, ChatWriteForbiddenError):
        return "ChatWriteForbidden", "No permission to post"
    if isinstance(exc, ChannelPrivateError):
        return "ChannelPrivate", "Channel is private"
    if isinstance(exc, UserBannedInChannelError):
        return "UserBannedInChannel", "User banned in channel"
    return type(exc).__name__, str(exc)


async def execute_job(job_id: int) -> bool:
    """Execute a single scheduled job. Returns True if sent successfully."""
    with get_db_context() as db:
        job = db.query(ScheduledJob).filter(ScheduledJob.id == job_id).first()
        if not job or job.status != JobStatus.PENDING:
            return False
        account = db.query(Account).filter(Account.id == job.account_id).first()
        target = db.query(ChatTarget).filter(ChatTarget.id == job.target_id).first()
        binding = db.query(AccountTargetBinding).filter(
            AccountTargetBinding.account_id == job.account_id,
            AccountTargetBinding.target_id == job.target_id
        ).first()

    if not account or not target or not binding:
        logger.warning("Job missing account/target/binding", job_id=job_id)
        return False
    if not binding.can_post:
        logger.info("Binding can_post=False, skipping", job_id=job_id)
        _mark_job_skipped(job_id, "Binding disabled")
        return False
    if account.status != AccountStatus.ACTIVE:
        logger.info("Account not active, skipping", job_id=job_id, status=account.status)
        _mark_job_skipped(job_id, "Account not active")
        return False

    # Get template body
    template_body = _get_template_body(job, account, target, binding)
    if not template_body:
        _mark_job_failed(job_id, "No template found")
        return False

    account_name = account.first_name or account.username or account.phone_number
    rendered = render_template(
        template_body,
        account_name=account_name,
        chat_title=target.title,
    )

    # Get client and send
    await client_manager.add_account(account)
    wrapper = await client_manager.get_client(account.id)
    if not wrapper:
        _mark_job_failed(job_id, "Failed to get client")
        return False
    if not wrapper.is_connected:
        if not await wrapper.connect():
            _mark_job_failed(job_id, "Failed to connect")
            return False

    entity = target.tg_id or target.username or target.invite_link
    if not entity:
        _mark_job_failed(job_id, "Target has no tg_id/username/link")
        return False

    try:
        msg = await wrapper.execute(
            wrapper.client.send_message,
            entity,
            rendered
        )
        _mark_job_sent(job_id, msg.id, rendered)
        logger.info("Message sent", job_id=job_id, tg_msg_id=msg.id)
        return True
    except FloodWaitError as e:
        _mark_job_failed(job_id, f"FloodWait {e.seconds}s")
        raise
    except (ChatWriteForbiddenError, ChannelPrivateError, UserBannedInChannelError) as e:
        code, msg = _map_error(e)
        _mark_job_failed(job_id, f"{code}: {msg}")
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
        _mark_job_failed(job_id, f"{code}: {msg}")
        return False


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
        job.status = JobStatus.SENT
        job.updated_at = datetime.utcnow()
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


def _mark_job_failed(job_id: int, error: str) -> None:
    with get_db_context() as db:
        job = db.query(ScheduledJob).filter(ScheduledJob.id == job_id).first()
        if not job:
            return
        job.status = JobStatus.FAILED
        job.attempts = (job.attempts or 0) + 1
        job.last_error = error
        job.updated_at = datetime.utcnow()
        d = MessageDelivery(
            job_id=job_id,
            account_id=job.account_id,
            target_id=job.target_id,
            type=job.type,
            status=DeliveryStatus.FAILED,
            error_message=error,
        )
        db.add(d)


def _mark_job_skipped(job_id: int, reason: str) -> None:
    with get_db_context() as db:
        job = db.query(ScheduledJob).filter(ScheduledJob.id == job_id).first()
        if job:
            job.status = JobStatus.SKIPPED
            job.last_error = reason
            job.updated_at = datetime.utcnow()
