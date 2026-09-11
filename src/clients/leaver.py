"""Canonical owner leave for Messages (Wave M).

Single Telethon leave primitive. Gated by ACTION_OWNER_CHAT_LEAVE /
MESSAGES_CHAT_LEAVE_ENABLED. Idempotent when already not a participant.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import structlog
from telethon.errors import (
    ChannelPrivateError,
    FloodWaitError,
    UserNotParticipantError,
)
from telethon.tl.functions.channels import LeaveChannelRequest
from telethon.tl.functions.messages import DeleteChatUserRequest
from telethon.tl.types import Channel, Chat

from src.clients.joiner import _entity_chat_type, _extract_invite_hash, _human_telethon_error
from src.clients.manager import client_manager
from src.clients.session_resolve import human_message_for_code
from src.core.database import get_db_context
from src.core.execution_guard import (
    ACTION_OWNER_CHAT_LEAVE,
    guard_blocked_join,
    require_execution_allowed,
)
from src.core.models import Account
from src.messaging.chat_ref import parse_chat_ref

logger = structlog.get_logger(__name__)

STATUS_LEFT = "left"
STATUS_ALREADY_LEFT = "already_left"
STATUS_FAILED = "failed"
STATUS_TEMP_ERROR = "temp_error"
STATUS_DENIED = "denied"


async def leave_ref_for_account(
    account_id: int,
    ref: str,
    *,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Leave a group/channel identified by username, invite, or numeric peer id."""
    parsed = parse_chat_ref(ref)
    base: Dict[str, Any] = {
        "account_id": int(account_id),
        "ref": (ref or "").strip(),
        "display": parsed.get("display") or (ref or "").strip(),
        "kind": parsed.get("kind"),
    }

    # Allow numeric peer id for leave of already-joined dialogs
    peer_numeric: Optional[int] = None
    if not parsed.get("ok"):
        raw = (ref or "").strip()
        if raw.lstrip("-").isdigit():
            peer_numeric = int(raw)
            base["kind"] = "peer_id"
            base["display"] = raw
        else:
            return {
                **base,
                "status": STATUS_FAILED,
                "error": parsed.get("reason") or "Invalid ref",
                "message": parsed.get("reason") or "Invalid ref",
            }

    with get_db_context() as db:
        blocked = require_execution_allowed(
            ACTION_OWNER_CHAT_LEAVE,
            account_id=int(account_id),
            db=db,
            dry_run=dry_run,
        )
    if blocked is not None:
        out = guard_blocked_join(blocked, base=base)
        out["status"] = STATUS_DENIED
        return out

    if dry_run:
        return {**base, "status": "dry_run", "message": "Dry-run: leave would be attempted", "ok": True}

    with get_db_context() as db:
        account = db.query(Account).filter(Account.id == account_id).first()
    if not account:
        return {
            **base,
            "status": STATUS_FAILED,
            "error": "Account not found",
            "message": "Account not found",
        }

    wrapper, fail_reason = await client_manager.add_account(account)
    if not wrapper:
        detail = human_message_for_code(fail_reason) if fail_reason else "Failed to get client"
        return {**base, "status": STATUS_FAILED, "error": detail, "message": detail}

    client = wrapper.client
    try:
        if peer_numeric is not None:
            entity = await client.get_entity(peer_numeric)
        elif parsed.get("invite_hash") or _extract_invite_hash(parsed.get("invite_link") or ""):
            # Invite alone cannot leave; need resolved entity from dialogs/cache
            return {
                **base,
                "status": STATUS_FAILED,
                "error": "leave_needs_joined_peer",
                "message": "To leave, open the joined chat from dialogs (or use @username).",
            }
        else:
            entity = await client.get_entity(parsed["username"])
    except Exception as e:
        return {
            **base,
            "status": STATUS_FAILED,
            "error": _human_telethon_error(e),
            "message": _human_telethon_error(e),
        }

    title = getattr(entity, "title", None) or getattr(entity, "username", None)
    chat_type = _entity_chat_type(entity)
    peer_id = getattr(entity, "id", None)

    try:
        if isinstance(entity, Channel):
            await client(LeaveChannelRequest(entity))
        elif isinstance(entity, Chat):
            me = await client.get_me()
            await client(DeleteChatUserRequest(entity.id, me))
        else:
            return {
                **base,
                "status": STATUS_FAILED,
                "error": "not_a_group_or_channel",
                "message": "Leave is only supported for groups and channels.",
                "title": title,
                "chat_type": chat_type,
                "peer_id": peer_id,
            }
        return {
            **base,
            "status": STATUS_LEFT,
            "message": "Left chat",
            "title": title,
            "chat_type": chat_type,
            "peer_id": peer_id,
        }
    except UserNotParticipantError:
        return {
            **base,
            "status": STATUS_ALREADY_LEFT,
            "message": "Already not a participant",
            "title": title,
            "chat_type": chat_type,
            "peer_id": peer_id,
        }
    except FloodWaitError as e:
        return {
            **base,
            "status": STATUS_TEMP_ERROR,
            "error": "FloodWaitError",
            "message": _human_telethon_error(e),
            "retry_after": getattr(e, "seconds", None),
        }
    except ChannelPrivateError as e:
        return {
            **base,
            "status": STATUS_FAILED,
            "error": _human_telethon_error(e),
            "message": _human_telethon_error(e),
        }
    except Exception as e:
        logger.error("leave_ref_for_account failed", account_id=account_id, error=str(e), exc_info=True)
        return {
            **base,
            "status": STATUS_FAILED,
            "error": _human_telethon_error(e),
            "message": _human_telethon_error(e),
        }
