"""Owner Messages chat resolve / join / leave orchestration (Wave M).

Canonical owner mutation surface for join/leave. Telethon joins go through
``clients.joiner.join_ref_for_account``; leaves through ``clients.leaver``.
"""
from __future__ import annotations

from typing import Any, Optional

import structlog
from telethon.errors import (
    ChannelPrivateError,
    FloodWaitError,
    UserBannedInChannelError,
    UserNotParticipantError,
)
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.tl.functions.messages import CheckChatInviteRequest
from telethon.tl.types import Channel, Chat, ChatInvite, ChatInviteAlready, User

from src.clients.joiner import (
    STATUS_ALREADY_JOINED,
    STATUS_JOINED,
    STATUS_JOIN_REQUESTED,
    STATUS_NO_PERMISSION,
    _can_post_heuristic,
    _entity_chat_type,
    _extract_invite_hash,
    _human_telethon_error,
    join_ref_for_account,
)
from src.clients.leaver import leave_ref_for_account
from src.clients.manager import client_manager
from src.clients.session_resolve import human_message_for_code
from src.core.database import get_db_context
from src.core.models import Account
from src.messaging.chat_flags import (
    messages_chat_join_enabled,
    messages_chat_leave_enabled,
)
from src.messaging.chat_ref import parse_chat_ref
from src.messaging.eligibility import evaluate_dm_account_eligibility

logger = structlog.get_logger(__name__)


class OwnerChatService:
    """Resolve → preview → confirm join/leave for Messages."""

    def resolve_input(self, raw: str) -> dict[str, Any]:
        """Pure parse/classify — no Telegram calls."""
        parsed = parse_chat_ref(raw)
        return {
            "ok": bool(parsed.get("ok")),
            "kind": parsed.get("kind"),
            "username": parsed.get("username"),
            "invite_link": parsed.get("invite_link"),
            "display": parsed.get("display"),
            "reason": parsed.get("reason"),
            "health": parsed.get("health"),
            "join_allowed": bool(parsed.get("ok")),
            "messages_chat_join_enabled": messages_chat_join_enabled(),
            "messages_chat_leave_enabled": messages_chat_leave_enabled(),
        }

    async def preview_async(self, account_id: int, raw: str) -> dict[str, Any]:
        """
        Non-mutating preview: parse + membership/entity probe when possible.
        Never joins.
        """
        parsed = parse_chat_ref(raw)
        base: dict[str, Any] = {
            "ok": True,
            "account_id": int(account_id),
            "input": (raw or "").strip(),
            "parsed": {
                "ok": bool(parsed.get("ok")),
                "kind": parsed.get("kind"),
                "username": parsed.get("username"),
                "invite_link": parsed.get("invite_link"),
                "display": parsed.get("display"),
                "reason": parsed.get("reason"),
            },
            "already_joined": False,
            "title": None,
            "chat_type": None,
            "peer_id": None,
            "public": parsed.get("kind") == "username",
            "can_post": None,
            "join_enabled": messages_chat_join_enabled(),
            "leave_enabled": messages_chat_leave_enabled(),
            "action_hint": "join",
        }

        if not parsed.get("ok"):
            base["ok"] = False
            base["error"] = "PEER_INVALID"
            base["message"] = parsed.get("reason") or "Invalid chat reference"
            base["action_hint"] = "fix"
            return base

        with get_db_context() as db:
            elig = evaluate_dm_account_eligibility(db, int(account_id))
            if not elig.eligible:
                base["ok"] = False
                base["error"] = elig.code
                base["message"] = elig.reason
                base["eligibility"] = elig.to_dict()
                base["action_hint"] = "fix"
                return base
            account = db.query(Account).filter(Account.id == account_id).first()

        if not account:
            base["ok"] = False
            base["error"] = "ACCOUNT_NOT_FOUND"
            base["message"] = "Account not found"
            return base

        wrapper, fail_reason = await client_manager.add_account(account)
        if not wrapper:
            detail = human_message_for_code(fail_reason) if fail_reason else "Failed to get client"
            base["ok"] = False
            base["error"] = "CLIENT_UNAVAILABLE"
            base["message"] = detail
            return base

        client = wrapper.client
        invite_hash = parsed.get("invite_hash") or _extract_invite_hash(
            parsed.get("invite_link") or ""
        )

        try:
            if invite_hash:
                inv = await client(CheckChatInviteRequest(invite_hash))
                if isinstance(inv, ChatInviteAlready):
                    entity = inv.chat
                    base["already_joined"] = True
                    base["action_hint"] = "open"
                    base["title"] = getattr(entity, "title", None)
                    base["peer_id"] = getattr(entity, "id", None)
                    base["chat_type"] = _entity_chat_type(entity)
                    ok_post, _ = _can_post_heuristic(entity)
                    base["can_post"] = ok_post
                    base["message"] = "Already joined"
                    return base
                if isinstance(inv, ChatInvite):
                    base["title"] = getattr(inv, "title", None)
                    base["chat_type"] = (
                        "channel" if getattr(inv, "broadcast", False) else "group"
                    )
                    base["public"] = False
                    base["message"] = "Invite preview — confirm to join"
                    base["action_hint"] = "join"
                    return base
                # ChatInvitePeek etc.
                base["title"] = getattr(inv, "title", None)
                base["message"] = "Invite preview — confirm to join"
                return base

            entity = await client.get_entity(parsed["username"])
            base["title"] = (
                getattr(entity, "title", None)
                or getattr(entity, "username", None)
                or parsed.get("display")
            )
            base["peer_id"] = getattr(entity, "id", None)
            base["chat_type"] = _entity_chat_type(entity)
            base["public"] = True

            if isinstance(entity, User):
                base["already_joined"] = True
                base["action_hint"] = "open"
                base["message"] = "This is a user/bot — open conversation (no join)"
                base["join_allowed"] = False
                return base

            if isinstance(entity, Channel):
                try:
                    await client(GetParticipantRequest(entity, await client.get_me()))
                    base["already_joined"] = True
                    base["action_hint"] = "open"
                    base["message"] = "Already joined"
                    ok_post, _ = _can_post_heuristic(entity)
                    base["can_post"] = ok_post
                    return base
                except UserNotParticipantError:
                    base["already_joined"] = False
                    base["action_hint"] = "join"
                    base["message"] = "Not a member — confirm to join"
                    return base
                except UserBannedInChannelError:
                    base["ok"] = False
                    base["error"] = "BANNED"
                    base["message"] = "Account is banned in this chat"
                    base["action_hint"] = "fix"
                    return base

            if isinstance(entity, Chat):
                # Basic groups: if get_entity worked we typically have access
                base["already_joined"] = True
                base["action_hint"] = "open"
                base["message"] = "Already accessible"
                return base

            base["message"] = "Resolved — confirm to join"
            return base

        except FloodWaitError as e:
            base["ok"] = False
            base["error"] = "FloodWaitError"
            base["message"] = _human_telethon_error(e)
            base["retry_after"] = getattr(e, "seconds", None)
            base["action_hint"] = "fix"
            return base
        except ChannelPrivateError:
            base["ok"] = False
            base["error"] = "PRIVATE"
            base["message"] = "Chat is private — provide a valid invite link"
            base["action_hint"] = "fix"
            return base
        except Exception as e:
            logger.info("owner_chat_preview_failed", account_id=account_id, error=str(e))
            base["ok"] = False
            base["error"] = "RESOLVE_FAILED"
            base["message"] = _human_telethon_error(e)
            base["action_hint"] = "fix"
            return base

    async def join_async(
        self,
        account_id: int,
        raw: str,
        *,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Execute join after explicit confirm. Idempotent if already member."""
        if not confirm:
            return {
                "ok": False,
                "error": "CONFIRM_REQUIRED",
                "message": "Join requires explicit confirmation.",
                "status": "failed",
            }

        with get_db_context() as db:
            elig = evaluate_dm_account_eligibility(db, int(account_id))
            if not elig.eligible:
                return {
                    "ok": False,
                    "error": elig.code,
                    "message": elig.reason,
                    "status": "failed",
                    "eligibility": elig.to_dict(),
                }

        # Short-circuit when preview says already member (no duplicate mutation)
        preview = await self.preview_async(account_id, raw)
        if preview.get("already_joined") and preview.get("action_hint") == "open":
            peer = None
            if preview.get("peer_id") is not None:
                peer = str(preview["peer_id"])
            uname = (preview.get("parsed") or {}).get("username")
            if uname:
                peer = f"@{uname}"
            return {
                "ok": True,
                "status": STATUS_ALREADY_JOINED,
                "message": "Already joined",
                "already_member": True,
                "title": preview.get("title"),
                "chat_type": preview.get("chat_type"),
                "peer_id": preview.get("peer_id"),
                "peer": peer,
                "can_post": preview.get("can_post"),
                "open_chat": True,
            }

        result = await join_ref_for_account(int(account_id), raw)
        status = result.get("status")
        ok = status in {
            STATUS_JOINED,
            STATUS_ALREADY_JOINED,
            STATUS_JOIN_REQUESTED,
            STATUS_NO_PERMISSION,
        }
        peer = None
        if result.get("peer_id") is not None:
            peer = str(result["peer_id"])
        parsed = parse_chat_ref(raw)
        if parsed.get("username"):
            peer = f"@{parsed['username']}"

        return {
            "ok": ok,
            "status": status,
            "message": result.get("message"),
            "error": result.get("error"),
            "error_code": result.get("error_code"),
            "already_member": bool(result.get("already_member"))
            or status == STATUS_ALREADY_JOINED,
            "title": result.get("title"),
            "chat_type": result.get("chat_type"),
            "peer_id": result.get("peer_id"),
            "peer": peer,
            "can_post": result.get("can_post"),
            "open_chat": ok and status != STATUS_JOIN_REQUESTED,
            "execution_guard": result.get("execution_guard"),
            "retry_after": result.get("retry_after"),
        }

    async def leave_async(
        self,
        account_id: int,
        raw: str,
        *,
        confirm: bool = False,
    ) -> dict[str, Any]:
        if not confirm:
            return {
                "ok": False,
                "error": "CONFIRM_REQUIRED",
                "message": "Leave requires explicit confirmation.",
                "status": "failed",
            }

        with get_db_context() as db:
            elig = evaluate_dm_account_eligibility(db, int(account_id))
            if not elig.eligible:
                return {
                    "ok": False,
                    "error": elig.code,
                    "message": elig.reason,
                    "status": "failed",
                    "eligibility": elig.to_dict(),
                }

        result = await leave_ref_for_account(int(account_id), raw)
        status = result.get("status")
        ok = status in {"left", "already_left"}
        return {
            "ok": ok,
            "status": status,
            "message": result.get("message"),
            "error": result.get("error"),
            "error_code": result.get("error_code"),
            "title": result.get("title"),
            "chat_type": result.get("chat_type"),
            "peer_id": result.get("peer_id"),
            "execution_guard": result.get("execution_guard"),
            "retry_after": result.get("retry_after"),
        }
