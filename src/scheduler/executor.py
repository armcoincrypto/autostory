"""
Scheduler job executor - sends messages via Telethon
"""
import re
from datetime import datetime
from typing import Optional, Tuple, Any

from telethon import utils as tg_utils
from telethon.errors import (
    FloodWaitError,
    ChatWriteForbiddenError,
    ChannelPrivateError,
    UserBannedInChannelError,
    UserAlreadyParticipantError,
    InviteHashExpiredError,
    FrozenMethodInvalidError,
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

# Username-only links (t.me/Name) - extract clean username for Telethon
_USERNAME_RE = re.compile(r"(?:https?://)?(?:t\.me/|@)([a-zA-Z0-9_]+)", re.I)


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
    if isinstance(exc, FrozenMethodInvalidError):
        return "FrozenAccount", "Account is restricted. Join the group manually from your phone first, then try again."
    return type(exc).__name__, str(exc)


def _normalize_entity_ref(link_or_username: str) -> str:
    """Extract clean reference for Telethon: username only, or invite hash."""
    s = (link_or_username or "").strip()
    if not s:
        return s
    # Invite link t.me/+xxx or t.me/joinchat/xxx – return as-is for join, hash extracted elsewhere
    if "+" in s and "t.me" in s:
        return s
    if "joinchat/" in s:
        return s
    # Public: t.me/ChannelName or @ChannelName → ChannelName
    m = _USERNAME_RE.search(s)
    if m:
        return m.group(1)
    return s


def _extract_invite_hash(link: str) -> Optional[str]:
    """Extract invite hash from t.me/+HASH or t.me/joinchat/HASH."""
    if not link:
        return None
    if "joinchat/" in link:
        return link.split("joinchat/")[-1].split("?")[0].strip()
    if "+" in link and "t.me" in link:
        return link.split("+")[-1].split("?")[0].strip()
    return None


async def _ensure_joined(client, link_or_username: str) -> Optional[Any]:
    """
    Join a chat/channel before sending. Returns the entity (chat/channel) if resolved, else None.
    Required for groups and private channels.
    """
    link = (link_or_username or "").strip()
    if "joinchat/" in link:
        from telethon.tl.functions.messages import ImportChatInviteRequest
        hash_part = _extract_invite_hash(link)
        if hash_part:
            updates = await client(ImportChatInviteRequest(hash_part))
            if updates and hasattr(updates, "chats") and updates.chats:
                return updates.chats[0]
    elif "+" in link and "t.me" in link:
        from telethon.tl.functions.messages import ImportChatInviteRequest
        hash_part = _extract_invite_hash(link)
        if hash_part:
            updates = await client(ImportChatInviteRequest(hash_part))
            if updates and hasattr(updates, "chats") and updates.chats:
                return updates.chats[0]
    else:
        from telethon.tl.functions.channels import JoinChannelRequest
        name = _normalize_entity_ref(link)
        if name:
            updates = await client(JoinChannelRequest(name))
            if updates and hasattr(updates, "chats") and updates.chats:
                return updates.chats[0]
    return None


async def _resolve_entity_from_invite(client, invite_link: str) -> Optional[Any]:
    """
    When already in group (UserAlreadyParticipant), use CheckChatInvite to get the chat entity.
    Frozen accounts cannot use this — fall back to _find_entity_in_dialogs.
    """
    from telethon.tl.functions.messages import CheckChatInviteRequest
    hash_part = _extract_invite_hash(invite_link)
    if not hash_part:
        return None
    try:
        r = await client(CheckChatInviteRequest(hash_part))
        if r and hasattr(r, "chat"):
            return r.chat
    except FrozenMethodInvalidError:
        raise  # Caller will catch and use dialogs fallback
    return None


async def _find_entity_in_dialogs(client, target) -> Optional[Any]:
    """
    Find chat by iterating dialogs. Works when account has manually joined and
    we can't use CheckChatInvite (frozen) or get_input_entity (private group).
    Matches by target.title or by entity username. Title must match exactly for
    invite-link-only targets (Scheduler → Advanced → Targets → Edit to set title).
    """
    try:
        title_lower = (target.title or "").strip().lower()
        # For username links (t.me/Name) extract name; for invite links (+xxx) this stays empty
        username_ref = None
        raw_ref = target.username or ""
        if raw_ref and "+" not in str(raw_ref) and "joinchat" not in str(raw_ref):
            username_ref = _normalize_entity_ref(raw_ref).lower()
        async for dialog in client.iter_dialogs():
            if not dialog.entity:
                continue
            # Match by title (exact or contained) — primary for invite-link targets
            if title_lower and dialog.name:
                dname = dialog.name.strip().lower()
                if dname == title_lower or title_lower in dname:
                    return dialog.entity
            # Match by entity username
            if username_ref:
                ent = dialog.entity
                uname = (getattr(ent, "username", None) or "").lower()
                if uname == username_ref:
                    return dialog.entity
                # Fuzzy: username parts in dialog name (e.g. "binanceukrainian" → "Binance Ukrainian")
                if dialog.name and "_" in username_ref:
                    parts = [p for p in username_ref.replace("_", " ").split() if len(p) > 2]
                    if parts and all(p in dialog.name.lower() for p in parts):
                        return dialog.entity
        return None
    except Exception:
        return None


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
        _mark_job_failed(job_id, "No template found. Create an ACCOUNT-scoped PROMO template for this account.", "NoTemplate")
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
        _mark_job_failed(
            job_id,
            "Failed to get client. Add or re-add the account via Accounts. (Sessions synced from server may not work when running locally.)",
            "NoClient"
        )
        return False
    if not wrapper.is_connected:
        if not await wrapper.connect():
            _mark_job_failed(job_id, "Failed to connect. Account session may be expired.", "ConnectionFailed")
            return False

    # Resolve entity: tg_id > invite_link > username
    link_or_username = target.invite_link or target.username
    if not target.tg_id and not link_or_username:
        _mark_job_failed(job_id, "Target has no tg_id, invite_link, or username. Add target details.", "InvalidTarget")
        return False

    entity = None
    need_join = not target.tg_id and link_or_username

    if need_join:
        # Join first: invite links (t.me/+xxx) and public groups (t.me/Name). Account must be a member to send.
        join_target = target.invite_link or link_or_username
        try:
            entity = await _ensure_joined(wrapper.client, join_target)
        except FrozenMethodInvalidError:
            _disable_binding(account.id, target.id)
            _mark_job_failed(
                job_id,
                "Account is restricted. Join the group manually from your phone first, then try again.",
                "FrozenAccount",
            )
            return False
        except InviteHashExpiredError:
            _mark_job_failed(job_id, "Invite link has expired. Update the target with a new link.", "InviteExpired")
            return False
        except UserAlreadyParticipantError:
            # Already in: for invite links, use CheckChatInvite to get the chat; frozen → use dialogs
            if _extract_invite_hash(join_target):
                try:
                    entity = await _resolve_entity_from_invite(wrapper.client, join_target)
                except FrozenMethodInvalidError:
                    # Frozen accounts can't use CheckChatInvite; find in dialogs (user joined manually)
                    entity = await _find_entity_in_dialogs(wrapper.client, target)
                    if not entity:
                        _mark_job_failed(
                            job_id,
                            "Account is restricted. Ensure the group is in your Telegram chats and target title matches.",
                            "FrozenAccount",
                        )
                        return False
                except Exception:
                    entity = None
            else:
                entity = None  # Public group: JoinChannel raised, but we're already in — resolve by username
        except Exception as e:
            err_str = str(e).lower()
            # Try dialogs fallback when JoinChannel fails for username (user may have joined manually)
            if "username" in err_str and _extract_invite_hash(join_target) is None:
                entity = await _find_entity_in_dialogs(wrapper.client, target)
                if not entity:
                    _disable_binding(account.id, target.id)
                    code, msg = _map_error(e)
                    _mark_job_failed(job_id, f"Could not join: {msg}. Use invite link (t.me/+xxx) for private groups.", code)
                    return False
            else:
                if "username" in err_str or "invite" in err_str:
                    _disable_binding(account.id, target.id)
                code, msg = _map_error(e)
                _mark_job_failed(job_id, f"Could not join: {msg}", code)
                return False

    # Resolve entity for send: use from join, or get_input_entity
    if entity is None:
        if target.tg_id:
            entity = target.tg_id
        else:
            # Telethon prefers @username or t.me/username for channels; preserve original case
            raw = _normalize_entity_ref(link_or_username) or link_or_username
            if raw and not raw.startswith("@"):
                entity = f"@{raw}"  # @Username often resolves better for channels
            else:
                entity = raw or link_or_username

    resolved = None
    try:
        # entity may already be a Chat from CheckChatInvite — get_input_entity accepts Entity-like
        resolved = await wrapper.client.get_input_entity(entity)
    except Exception as e:
        # Fallback: find in dialogs when user manually joined (works for private groups / frozen)
        if "username" in str(e).lower() or "entity" in str(e).lower():
            found = await _find_entity_in_dialogs(wrapper.client, target)
            if found:
                try:
                    resolved = await wrapper.client.get_input_entity(found)
                except Exception:
                    resolved = found  # use entity directly if get_input_entity fails
        if resolved is None:
            code, msg = _map_error(e)
            hint = ""
            if "username" in str(e).lower() and link_or_username and "+" not in str(link_or_username):
                hint = " Use invite link (t.me/+xxx) for private groups."
            _disable_binding(account.id, target.id)
            _mark_job_failed(job_id, f"Could not resolve target: {msg}.{hint}", code or "ResolveFailed")
            return False

    try:
        tid = tg_utils.get_peer_id(resolved) if hasattr(tg_utils, "get_peer_id") else (getattr(resolved, "channel_id", None) or getattr(resolved, "chat_id", None))
        if not target.tg_id and tid:
            with get_db_context() as db:
                t = db.query(ChatTarget).filter(ChatTarget.id == target.id).first()
                if t:
                    t.tg_id = tid
    except Exception:
        pass

    # Log resolved chat for debugging (which account, which chat)
    chat_title_resolved = ""
    try:
        full_entity = await wrapper.client.get_entity(resolved)
        chat_title_resolved = getattr(full_entity, "title", None) or getattr(full_entity, "username", None) or str(resolved)
    except Exception:
        chat_title_resolved = str(resolved)
    try:
        tid_val = tg_utils.get_peer_id(resolved) if hasattr(tg_utils, "get_peer_id") else (getattr(resolved, "channel_id", None) or getattr(resolved, "chat_id", None))
    except Exception:
        tid_val = None
    logger.info(
        "Sending message",
        job_id=job_id,
        account_phone=account.phone_number,
        account_id=account.id,
        target_display=target.title or target.username or target.invite_link,
        resolved_chat=chat_title_resolved,
        resolved_peer_id=tid_val,
    )

    try:
        msg = await wrapper.execute(
            wrapper.client.send_message,
            resolved,
            rendered
        )
        _mark_job_sent(job_id, msg.id, rendered)
        logger.info(
            "Message sent",
            job_id=job_id,
            tg_msg_id=msg.id,
            account_phone=account.phone_number,
            resolved_chat=chat_title_resolved,
        )
        return True
    except FloodWaitError as e:
        code, msg = _map_error(e)
        _mark_job_failed(job_id, f"{msg}", code)
        raise
    except (ChatWriteForbiddenError, ChannelPrivateError, UserBannedInChannelError) as e:
        code, msg = _map_error(e)
        _mark_job_failed(job_id, msg, code)
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
        _mark_job_failed(job_id, msg, code)
        logger.exception("Job execution error", job_id=job_id, exc_info=e)
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


def _disable_binding(account_id: int, target_id: int) -> None:
    """Disable binding to stop retrying on persistent failures."""
    with get_db_context() as db:
        b = db.query(AccountTargetBinding).filter(
            AccountTargetBinding.account_id == account_id,
            AccountTargetBinding.target_id == target_id
        ).first()
        if b:
            b.can_post = False
            logger.info("Binding disabled", account_id=account_id, target_id=target_id)


def _mark_job_failed(job_id: int, error: str, error_code: Optional[str] = None) -> None:
    """Store FAILED delivery with error_code and error_message for API/UI display."""
    code = error_code or "SendFailed"
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
            error_code=code,
            error_message=error,
        )
        db.add(d)
        logger.warning("Job failed", job_id=job_id, error_code=code, error=error)


def _mark_job_skipped(job_id: int, reason: str) -> None:
    with get_db_context() as db:
        job = db.query(ScheduledJob).filter(ScheduledJob.id == job_id).first()
        if job:
            job.status = JobStatus.SKIPPED
            job.last_error = reason
            job.updated_at = datetime.utcnow()
