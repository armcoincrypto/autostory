"""
Per-account target join helper.

Used by the Scheduler UI when an operator saves a campaign:
for each (account_id, target_id) pair we try to make sure the account is
actually a member of the target chat/channel and has permission to post.
This avoids the common ChatWriteForbidden failure at first send time.

Design notes
------------
* Reuses ``client_manager.add_account()`` — exactly the same code path the
  executor uses (``src/scheduler/executor.py:80``). This guarantees the join
  attempt and the eventual scheduled send use the same connected session.
* Does NOT disconnect the client at the end. The wrapper stays in the manager's
  pool so the executor can use it immediately afterwards without reconnecting.
* Does not modify scheduler/worker logic. The only DB write it performs is
  best-effort (cache resolved ``tg_id`` / ``title`` on the target row, and
  flip ``binding.can_post`` to False when the join confirms there is no
  permission to post — mirroring what ``executor.py`` already does after a
  ChatWriteForbidden at send time).
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

import structlog
from telethon.errors import (
    ChannelPrivateError,
    ChatAdminRequiredError,
    ChatWriteForbiddenError,
    FloodWaitError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    InviteRequestSentError,
    UserAlreadyParticipantError,
    UserBannedInChannelError,
)
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.tl.types import Channel

from src.clients.manager import client_manager
from src.clients.session_resolve import human_message_for_code
from src.clients.target_health import (
    classify_target,
    entity_probe_chain,
    is_health_allowed_for_binding,
    resolve_executor_entity,
)
from src.core.database import get_db_context
from src.core.models import Account
from src.core.scheduler_models import AccountTargetBinding, ChatTarget

logger = structlog.get_logger(__name__)

# Per-target outcome codes. Stable contract for the UI.
STATUS_ALREADY_JOINED = "already_joined"
STATUS_JOINED = "joined"
STATUS_JOIN_REQUESTED = "join_requested"   # Telegram accepted a join request,
                                           # waiting for chat admin approval.
                                           # Not a failure.
STATUS_NO_PERMISSION = "no_permission_to_post"
STATUS_TEMP_ERROR = "temp_error"
STATUS_FAILED = "failed"


def _extract_invite_hash(link: str) -> Optional[str]:
    """Return the hash part from t.me/joinchat/<hash> or t.me/+<hash> URLs."""
    if not link:
        return None
    s = link.strip()
    try:
        u = urlparse(s if "://" in s else "https://" + s)
        path = u.path or ""
    except Exception:
        path = s
    if "joinchat/" in path:
        return path.split("joinchat/", 1)[1].strip("/").split("?", 1)[0]
    # Path begins with "/+HASH" for new-style invite links
    if path.startswith("/+"):
        return path[2:].split("?", 1)[0]
    if s.startswith("+"):
        return s[1:]
    return None


def _can_post_heuristic(entity: Any) -> Tuple[bool, str]:
    """
    Best-effort post-permission check using only data Telethon caches on the
    entity returned by ``get_entity`` / a successful join.

    Returns ``(can_post, reason)``. A False result is conservative: we'd
    rather flag a channel as no-post than let the worker hit
    ChatWriteForbidden on the first scheduled send.
    """
    try:
        broadcast = bool(getattr(entity, "broadcast", False))
        is_creator = bool(getattr(entity, "creator", False))
        admin_rights = getattr(entity, "admin_rights", None)
        if broadcast and not is_creator and not admin_rights:
            return False, "Broadcast channel: only admins can post"
        banned = getattr(entity, "default_banned_rights", None)
        if banned is not None and getattr(banned, "send_messages", False):
            # Group with messages disabled for regular members
            if not is_creator and not admin_rights:
                return False, "Group default rights forbid sending messages"
    except Exception:
        # Don't block save just because permission introspection failed.
        return True, ""
    return True, ""


def _human_telethon_error(exc: Exception) -> str:
    """Map Telethon exceptions to short operator-facing strings."""
    if isinstance(exc, FloodWaitError):
        return f"FloodWait {exc.seconds}s — Telegram is rate-limiting this account"
    if isinstance(exc, UserAlreadyParticipantError):
        return "Already a participant"
    if isinstance(exc, InviteRequestSentError):
        return "Join request sent — waiting for admin approval"
    if isinstance(exc, InviteHashExpiredError):
        return "Invite link expired"
    if isinstance(exc, InviteHashInvalidError):
        return "Invite link is invalid"
    if isinstance(exc, ChannelPrivateError):
        return "Channel is private and account has no access"
    if isinstance(exc, UserBannedInChannelError):
        return "Account is banned in this channel"
    if isinstance(exc, ChatAdminRequiredError):
        return "Admin rights required"
    if isinstance(exc, ChatWriteForbiddenError):
        return "No permission to post"
    return f"{type(exc).__name__}: {exc}"


def _cache_target_metadata(target_id: int, entity: Any) -> None:
    """Best-effort: persist resolved tg_id / title on the ChatTarget row."""
    try:
        with get_db_context() as db:
            row = db.query(ChatTarget).filter(ChatTarget.id == target_id).first()
            if not row:
                return
            tg_id = getattr(entity, "id", None)
            title = getattr(entity, "title", None) or getattr(entity, "username", None)
            changed = False
            if tg_id and not row.tg_id:
                row.tg_id = int(tg_id)
                changed = True
            elif tg_id and row.tg_id and isinstance(entity, Channel):
                # Replace a stale positive id (often mis-cached as PeerUser) with the
                # real supergroup id from Telegram after a successful resolve/join.
                try:
                    ni = int(tg_id)
                    oi = int(row.tg_id)
                    if oi > 0 and ni < 0 and oi != ni:
                        row.tg_id = ni
                        changed = True
                except (TypeError, ValueError):
                    pass
            if title and not row.title:
                row.title = str(title)[:255]
                changed = True
            if changed:
                row.is_verified = True
                from datetime import datetime as _dt
                row.verified_at = _dt.utcnow()
    except Exception as e:
        # Caching is opportunistic; never break the join flow because of it.
        logger.warning("Failed to cache target metadata", target_id=target_id, error=str(e))


def _set_binding_can_post(account_id: int, target_id: int, can_post: bool) -> None:
    """Mirror ``executor.py``'s behavior: when we know posting is impossible,
    flip the binding so the worker won't even try."""
    try:
        with get_db_context() as db:
            b = db.query(AccountTargetBinding).filter(
                AccountTargetBinding.account_id == account_id,
                AccountTargetBinding.target_id == target_id,
            ).first()
            if b and bool(b.can_post) != bool(can_post):
                b.can_post = bool(can_post)
    except Exception as e:
        logger.warning(
            "Failed to update binding.can_post",
            account_id=account_id,
            target_id=target_id,
            error=str(e),
        )


async def join_target_for_account(account_id: int, target_id: int) -> Dict[str, Any]:
    """
    Try to make ``account_id`` a writable member of ``target_id``.

    Returns a dict with stable keys for the UI:
        {
          "target_id": int,
          "target_label": str,                 # username | invite_link | tg_id | id
          "status": "already_joined" | "joined" | "no_permission_to_post" | "failed",
          "message": str,                      # human-readable summary
          "error": Optional[str],              # only present when status == failed
        }
    """
    with get_db_context() as db:
        account = db.query(Account).filter(Account.id == account_id).first()
        target = db.query(ChatTarget).filter(ChatTarget.id == target_id).first()

    base = {
        "target_id": target_id,
        "target_label": (
            (target and (target.username or target.invite_link or (target.tg_id and str(target.tg_id))))
            or (target and f"target #{target.id}")
            or f"target #{target_id}"
        ),
    }
    if not account:
        return {**base, "status": STATUS_FAILED, "error": "Account not found", "message": "Account not found"}
    if not target:
        return {**base, "status": STATUS_FAILED, "error": "Target not found", "message": "Target not found"}

    from src.core.execution_guard import (
        ACTION_TELEGRAM_JOIN,
        guard_blocked_join,
        require_execution_allowed,
    )

    with get_db_context() as db:
        blocked = require_execution_allowed(
            ACTION_TELEGRAM_JOIN,
            account_id=int(account_id),
            target_id=int(target_id),
            db=db,
        )
    if blocked is not None:
        logger.warning(
            "joiner_blocked_execution_guard",
            account_id=int(account_id),
            target_id=int(target_id),
            reason=blocked.reason_code,
        )
        return guard_blocked_join(blocked, base=base)

    # Short-circuit targets the classifier knows are intrinsically unusable
    # (e.g. t.me/c/<id>/<msg> message links, missing handles, malformed
    # usernames). Avoids a useless Telegram round-trip and gives the operator
    # an actionable error instead of a generic "ChannelPrivate".
    health = classify_target(target)
    if not is_health_allowed_for_binding(health.get("health") or ""):
        return {
            **base,
            "status": STATUS_FAILED,
            "error": health.get("reason") or "Target cannot be joined",
            "message": health.get("reason") or "Target cannot be joined or sent to",
        }

    wrapper, fail_reason = await client_manager.add_account(account)
    if not wrapper:
        detail = human_message_for_code(fail_reason) if fail_reason else "Failed to get client"
        return {**base, "status": STATUS_FAILED, "error": detail, "message": detail}

    client = wrapper.client
    invite_hash = _extract_invite_hash(target.invite_link or "")

    # Strategy:
    # 1) If we have an invite hash, use ImportChatInviteRequest. This works for
    #    private chats where get_entity() would raise ChannelPrivateError.
    # 2) Else resolve the entity and JoinChannelRequest. Use the same ref policy as
    #    ``membership_check`` / ``executor.resolve_executor_entity`` (username /
    #    invite before raw ``tg_id``) so a stale positive user id cannot be used
    #    as ``PeerUser`` while @username still resolves the real supergroup.
    try:
        if invite_hash:
            try:
                upd = await client(ImportChatInviteRequest(invite_hash))
                # ImportChatInviteRequest returns Updates whose .chats[0] is the chat
                entity = (upd.chats or [None])[0]
                if entity is None:
                    ref_fb = resolve_executor_entity(target)
                    if ref_fb is not None:
                        try:
                            entity = await client.get_entity(ref_fb)
                        except Exception:
                            entity = None
                _cache_target_metadata(target.id, entity) if entity else None
                ok_post, reason = _can_post_heuristic(entity) if entity else (True, "")
                if not ok_post:
                    _set_binding_can_post(account.id, target.id, False)
                    return {**base, "status": STATUS_NO_PERMISSION, "message": f"Joined but cannot post — {reason}"}
                return {**base, "status": STATUS_JOINED, "message": "Joined via invite link"}
            except InviteRequestSentError:
                # Chat requires admin approval. Not a failure — the request is queued.
                # We do NOT flip can_post here: pending state is unknown and we want
                # the worker to surface the actual outcome at first send time.
                return {**base, "status": STATUS_JOIN_REQUESTED,
                        "message": "Waiting for admin approval"}
            except UserAlreadyParticipantError:
                # Already in via invite — verify permissions on best effort
                try:
                    ref_u = resolve_executor_entity(target)
                    entity = await client.get_entity(ref_u) if ref_u is not None else None
                except Exception:
                    entity = None
                ok_post, reason = _can_post_heuristic(entity) if entity else (True, "")
                if entity:
                    _cache_target_metadata(target.id, entity)
                if not ok_post:
                    _set_binding_can_post(account.id, target.id, False)
                    return {**base, "status": STATUS_NO_PERMISSION, "message": f"Already joined but cannot post — {reason}"}
                return {**base, "status": STATUS_ALREADY_JOINED, "message": "Already a member"}
            except FloodWaitError as e:
                return {
                    **base,
                    "status": STATUS_TEMP_ERROR,
                    "error": "FloodWaitError",
                    "message": _human_telethon_error(e),
                }

        # Public-target path: resolve and JoinChannelRequest (same ref chain as membership).
        chain = entity_probe_chain(target)
        if not chain:
            return {**base, "status": STATUS_FAILED, "error": "No username, invite link, or tg_id on target",
                    "message": "Target has no usable handle"}

        entity = None
        winning_ref: Any = None
        last_exc: Optional[Exception] = None
        saw_private = False
        for ref in chain:
            try:
                entity = await client.get_entity(ref)
                winning_ref = ref
                last_exc = None
                break
            except ChannelPrivateError as e:
                last_exc = e
                saw_private = True
                continue
            except Exception as e:
                last_exc = e
                continue

        if entity is None:
            if saw_private and last_exc:
                return {**base, "status": STATUS_FAILED, "error": "Channel is private (need invite link)",
                        "message": "Channel is private — provide an invite link"}
            return {**base, "status": STATUS_FAILED, "error": _human_telethon_error(last_exc) if last_exc else "resolve_failed",
                    "message": _human_telethon_error(last_exc) if last_exc else "Could not resolve target"}

        if (
            winning_ref is not None
            and chain
            and winning_ref != chain[0]
            and (getattr(target, "username", None) or getattr(target, "invite_link", None))
        ):
            _cache_target_metadata(target.id, entity)
            logger.info(
                "target_auto_healed_cached_id",
                target_id=target.id,
                account_id=account_id,
                context="joiner",
            )

        try:
            await client(JoinChannelRequest(entity))
            joined_status = STATUS_JOINED
            joined_message = "Joined"
        except UserAlreadyParticipantError:
            joined_status = STATUS_ALREADY_JOINED
            joined_message = "Already a member"
        except InviteRequestSentError:
            # Public group/channel that requires admin approval to join.
            # Not a failure — Telegram queued the request. Cache what we know
            # about the entity but skip the post-permission heuristic, since
            # the account isn't a member yet.
            _cache_target_metadata(target.id, entity)
            return {**base, "status": STATUS_JOIN_REQUESTED,
                    "message": "Waiting for admin approval"}
        except (UserBannedInChannelError, ChannelPrivateError, ChatAdminRequiredError) as e:
            return {**base, "status": STATUS_FAILED, "error": _human_telethon_error(e),
                    "message": _human_telethon_error(e)}
        except FloodWaitError as e:
            return {
                **base,
                "status": STATUS_TEMP_ERROR,
                "error": "FloodWaitError",
                "message": _human_telethon_error(e),
            }

        _cache_target_metadata(target.id, entity)
        ok_post, reason = _can_post_heuristic(entity)
        if not ok_post:
            _set_binding_can_post(account.id, target.id, False)
            return {**base, "status": STATUS_NO_PERMISSION,
                    "message": f"{joined_message} but cannot post — {reason}"}
        return {**base, "status": joined_status, "message": joined_message}

    except sqlite3.OperationalError as e:
        if "locked" in str(e).lower():
            return {
                **base,
                "status": STATUS_TEMP_ERROR,
                "error": "session_db_locked",
                "message": "Database is locked — retry shortly (session contention)",
            }
        msg = _human_telethon_error(e)
        return {**base, "status": STATUS_TEMP_ERROR, "error": "database_error", "message": msg}
    except Exception as e:
        # Fail-safe: never let an unexpected exception break the API call.
        logger.error(
            "join_target_for_account failed",
            account_id=account_id,
            target_id=target_id,
            error=str(e),
            exc_info=True,
        )
        msg = _human_telethon_error(e)
        return {**base, "status": STATUS_FAILED, "error": msg, "message": msg}
