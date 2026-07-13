"""
Read-only Telegram membership probe for (account, target) pairs.

Uses the same ``client_manager.add_account`` path as the executor and joiner.
Does NOT call JoinChannelRequest / ImportChatInviteRequest — no membership mutation.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import structlog
from telethon.errors import (
    ChannelPrivateError,
    FloodWaitError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    UserBannedInChannelError,
    UserNotParticipantError,
)
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.tl.functions.messages import CheckChatInviteRequest
from telethon.tl.types import Channel, Chat, ChatInvite, ChatInviteAlready, ChatInvitePeek

from src.clients.joiner import (
    _cache_target_metadata,
    _can_post_heuristic,
    _extract_invite_hash,
    _human_telethon_error,
)
from src.clients.manager import client_manager
from src.clients.session_resolve import human_message_for_code
from src.clients.target_health import (
    HEALTH_INVALID,
    HEALTH_NEEDS_REPAIR,
    classify_target,
    entity_probe_chain,
)
from src.core.database import get_db_context
from src.core.models import Account
from src.core.datetime_utc import to_utc_iso_z
from src.core.scheduler_models import AccountTargetMembershipProbe, ChatTarget

logger = structlog.get_logger(__name__)

# API contract (stable for Scheduler UI)
MEMBERSHIP_JOINED = "joined"
MEMBERSHIP_NOT_JOINED = "not_joined"
MEMBERSHIP_PENDING_APPROVAL = "pending_approval"
MEMBERSHIP_BANNED = "banned"
MEMBERSHIP_NO_ACCESS = "no_access"
MEMBERSHIP_UNRESOLVED_TARGET = "unresolved_target"
MEMBERSHIP_NO_PERMISSION_TO_POST = "no_permission_to_post"
MEMBERSHIP_UNKNOWN = "unknown"
MEMBERSHIP_ERROR = "error"

# Short-lived cache (seconds) — avoids hammering Telegram on repeated UI clicks.
DEFAULT_PROBE_CACHE_TTL_SEC = 120

# Never block a batch or a single target indefinitely (Telethon slow paths, FloodWait, etc.).
MEMBERSHIP_PER_TARGET_TIMEOUT_SEC = 22.0
MEMBERSHIP_BATCH_WALL_SEC = 95.0


def _base_row(target: Optional[ChatTarget], target_id: int) -> Dict[str, Any]:
    label = None
    if target:
        label = target.username or target.invite_link or (target.tg_id and str(target.tg_id))
    return {
        "target_id": target_id,
        "target_label": label or (f"target #{target_id}" if target else f"target #{target_id}"),
    }


async def _participant_status_for_channel(
    client: Any,
    channel: Channel,
) -> Tuple[str, Optional[bool], str]:
    """
    After confirming ``channel`` is a Channel megagroup/supergroup/broadcast,
    ask Telegram whether *this* account is a participant and infer postability.
    """
    try:
        await client(GetParticipantRequest(channel, await client.get_me()))
    except UserNotParticipantError:
        return MEMBERSHIP_NOT_JOINED, False, "Not a member of this channel"
    except UserBannedInChannelError as e:
        return MEMBERSHIP_BANNED, False, _human_telethon_error(e)
    except ChannelPrivateError as e:
        return MEMBERSHIP_NO_ACCESS, False, _human_telethon_error(e)
    except Exception as e:
        return MEMBERSHIP_ERROR, None, _human_telethon_error(e)

    ok_post, reason = _can_post_heuristic(channel)
    if not ok_post:
        return MEMBERSHIP_NO_PERMISSION_TO_POST, False, reason or "Member but cannot post in this channel"
    return MEMBERSHIP_JOINED, True, "Already a member and can post"


async def check_target_membership_for_account(account_id: int, target_id: int) -> Dict[str, Any]:
    """
    Probe Telegram for membership only (no join RPC).

    Returns dict: target_id, target_label, status, can_post (bool|null), message, error?
    """
    with get_db_context() as db:
        account = db.query(Account).filter(Account.id == account_id).first()
        target = db.query(ChatTarget).filter(ChatTarget.id == target_id).first()

    out = _base_row(target, target_id)
    if not account:
        return {
            **out,
            "status": MEMBERSHIP_ERROR,
            "can_post": None,
            "message": "Account not found",
            "error": "account_not_found",
        }
    if not target:
        return {
            **out,
            "status": MEMBERSHIP_ERROR,
            "can_post": None,
            "message": "Target not found",
            "error": "target_not_found",
        }

    health = classify_target(target)
    if health.get("health") == HEALTH_INVALID:
        return {
            **out,
            "status": MEMBERSHIP_UNRESOLVED_TARGET,
            "can_post": None,
            "message": health.get("reason") or "Target is invalid or unusable",
            "error": "invalid_target",
        }
    if health.get("health") == HEALTH_NEEDS_REPAIR:
        return {
            **out,
            "status": MEMBERSHIP_UNRESOLVED_TARGET,
            "can_post": None,
            "message": health.get("reason") or "Target needs a @username or invite before membership can be checked",
            "error": "needs_repair",
        }

    wrapper, fail_reason = await client_manager.connect_account(int(account_id))
    if not wrapper:
        detail = human_message_for_code(fail_reason) if fail_reason else "Failed to get Telegram client"
        return {
            **out,
            "status": MEMBERSHIP_ERROR,
            "can_post": None,
            "message": detail,
            "error": fail_reason or "client_unavailable",
        }

    client = wrapper.client
    invite_hash = _extract_invite_hash(target.invite_link or "")

    try:
        # ---- Invite-link path (read-only check first) ----
        if invite_hash:
            try:
                inv = await client(CheckChatInviteRequest(invite_hash))
            except (InviteHashInvalidError, InviteHashExpiredError) as e:
                return {
                    **out,
                    "status": MEMBERSHIP_UNRESOLVED_TARGET,
                    "can_post": None,
                    "message": _human_telethon_error(e),
                    "error": type(e).__name__,
                }
            except FloodWaitError as e:
                return {
                    **out,
                    "status": MEMBERSHIP_ERROR,
                    "can_post": None,
                    "message": _human_telethon_error(e),
                    "error": "FloodWaitError",
                }
            except Exception as e:
                return {
                    **out,
                    "status": MEMBERSHIP_ERROR,
                    "can_post": None,
                    "message": _human_telethon_error(e),
                    "error": type(e).__name__,
                }

            if isinstance(inv, ChatInviteAlready):
                chat = inv.chat
                if isinstance(chat, Channel):
                    st, cp, msg = await _participant_status_for_channel(client, chat)
                    return {**out, "status": st, "can_post": cp, "message": msg}
                return {
                    **out,
                    "status": MEMBERSHIP_JOINED,
                    "can_post": None,
                    "message": "Already in this chat (invite check); post permission not evaluated for this chat type",
                }

            if isinstance(inv, ChatInvitePeek):
                inner = getattr(inv, "chat", None)
                if isinstance(inner, Channel):
                    st, cp, msg = await _participant_status_for_channel(client, inner)
                    return {**out, "status": st, "can_post": cp, "message": msg}
                return {
                    **out,
                    "status": MEMBERSHIP_NOT_JOINED,
                    "can_post": False,
                    "message": "Not a member (invite preview only)",
                }

            if isinstance(inv, ChatInvite):
                if bool(getattr(inv, "request_needed", None)):
                    return {
                        **out,
                        "status": MEMBERSHIP_PENDING_APPROVAL,
                        "can_post": False,
                        "message": "Not a full member yet — this group uses admin-approved joins. If you already requested access, wait for an admin.",
                    }
                return {
                    **out,
                    "status": MEMBERSHIP_NOT_JOINED,
                    "can_post": False,
                    "message": "Not a member of this invite link chat",
                }

            return {
                **out,
                "status": MEMBERSHIP_UNKNOWN,
                "can_post": None,
                "message": f"Unexpected invite response: {type(inv).__name__}",
            }

        # ---- Public handle / tg_id path ----
        # Match ``resolve_executor_entity`` / executor: prefer a valid @username (or real
        # invite URL) over a cached numeric ``tg_id``. Cached ids are often channel peers
        # mis-read as ``PeerUser`` by Telethon when passed as a bare positive int.
        chain = entity_probe_chain(target)
        if not chain:
            return {
                **out,
                "status": MEMBERSHIP_UNRESOLVED_TARGET,
                "can_post": None,
                "message": "No username, invite link, or tg_id on this target",
                "error": "no_handle",
            }

        entity = None
        last_exc: Optional[Exception] = None
        winning_ref: Any = None
        saw_channel_private = False
        for ref in chain:
            try:
                entity = await client.get_entity(ref)
                winning_ref = ref
                last_exc = None
                break
            except ChannelPrivateError as e:
                last_exc = e
                saw_channel_private = True
                continue
            except Exception as e:
                last_exc = e
                continue

        if entity is None:
            if saw_channel_private and last_exc:
                return {
                    **out,
                    "status": MEMBERSHIP_NO_ACCESS,
                    "can_post": False,
                    "message": _human_telethon_error(last_exc),
                    "error": "ChannelPrivateError",
                }
            return {
                **out,
                "status": MEMBERSHIP_UNRESOLVED_TARGET,
                "can_post": None,
                "message": _human_telethon_error(last_exc) if last_exc else "Could not resolve entity",
                "error": type(last_exc).__name__ if last_exc else "resolve_failed",
            }

        # Optional auto-heal: stale tg_id failed earlier in chain; username/invite succeeded.
        if (
            winning_ref is not None
            and chain
            and winning_ref != chain[0]
            and (getattr(target, "username", None) or getattr(target, "invite_link", None))
        ):
            _cache_target_metadata(target_id, entity)
            logger.info(
                "target_auto_healed_cached_id",
                target_id=target_id,
                account_id=account_id,
                winning_ref_type=type(winning_ref).__name__,
                had_cached_tg_id=getattr(target, "tg_id", None) is not None,
            )

        if isinstance(entity, Channel):
            st, cp, msg = await _participant_status_for_channel(client, entity)
            return {**out, "status": st, "can_post": cp, "message": msg}

        if isinstance(entity, Chat):
            return {
                **out,
                "status": MEMBERSHIP_UNKNOWN,
                "can_post": None,
                "message": "Basic group — use join flow to confirm membership; read-only channel probe not applicable",
            }

        return {
            **out,
            "status": MEMBERSHIP_UNKNOWN,
            "can_post": None,
            "message": f"Unexpected entity type: {type(entity).__name__}",
        }

    except Exception as e:
        logger.error(
            "membership_check_failed",
            account_id=account_id,
            target_id=target_id,
            error=str(e),
            exc_info=True,
        )
        return {
            **out,
            "status": MEMBERSHIP_ERROR,
            "can_post": None,
            "message": _human_telethon_error(e),
            "error": type(e).__name__,
        }
    finally:
        try:
            await client_manager.remove_account(int(account_id))
        except Exception as exc:
            logger.warning(
                "membership_check_remove_account_failed",
                account_id=account_id,
                error=str(exc),
            )


def _membership_target_timeout_row(account_id: int, target_id: int, *, wall: bool = False) -> Dict[str, Any]:
    code = "batch_wall_timeout" if wall else "target_timeout"
    msg = (
        "Batch time budget exhausted before this target could be checked — retry membership"
        if wall
        else "This target’s Telegram check exceeded the per-target time limit — retry"
    )
    return {
        "target_id": int(target_id),
        "target_label": f"target #{target_id}",
        "status": MEMBERSHIP_ERROR,
        "can_post": None,
        "message": msg,
        "error": code,
        "cached": False,
    }


async def check_targets_membership_sequential(
    account_id: int,
    target_ids: List[int],
    *,
    throttle_sec: float = 0.35,
    per_target_timeout: float = MEMBERSHIP_PER_TARGET_TIMEOUT_SEC,
    batch_wall_sec: float = MEMBERSHIP_BATCH_WALL_SEC,
) -> List[Dict[str, Any]]:
    """
    Run probes sequentially with per-target ``asyncio.wait_for`` caps and an overall
    wall clock so the web tier never hangs on one bad target.
    """
    results: List[Dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + float(batch_wall_sec)
    for i, tid in enumerate(target_ids):
        if i:
            await asyncio.sleep(throttle_sec)
        remain = deadline - loop.time()
        if remain < 0.5:
            for rest in target_ids[i:]:
                results.append(_membership_target_timeout_row(account_id, rest, wall=True))
            break
        budget = min(float(per_target_timeout), remain)
        try:
            row = await asyncio.wait_for(
                check_target_membership_for_account(account_id, tid),
                timeout=budget,
            )
        except asyncio.TimeoutError:
            row = _membership_target_timeout_row(account_id, tid, wall=False)
        results.append(row)
    return results


def read_cached_probes(
    db: Any,
    account_id: int,
    target_ids: List[int],
    *,
    ttl_sec: int = DEFAULT_PROBE_CACHE_TTL_SEC,
) -> Dict[int, Dict[str, Any]]:
    """Return target_id -> result dict for rows fresher than ttl_sec."""
    if not target_ids:
        return {}
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=ttl_sec)
    rows = (
        db.query(AccountTargetMembershipProbe)
        .filter(
            AccountTargetMembershipProbe.account_id == account_id,
            AccountTargetMembershipProbe.target_id.in_(target_ids),
            AccountTargetMembershipProbe.checked_at >= cutoff,
        )
        .all()
    )
    out: Dict[int, Dict[str, Any]] = {}
    for r in rows:
        out[int(r.target_id)] = {
            "target_id": int(r.target_id),
            "target_label": None,
            "status": r.status,
            "can_post": r.can_post,
            "message": r.message or "",
            "cached": True,
            "checked_at": to_utc_iso_z(r.checked_at),
        }
    return out


def upsert_membership_probe(
    db: Any,
    account_id: int,
    result: Dict[str, Any],
    *,
    checked_at: Optional[datetime] = None,
) -> None:
    """Persist latest probe for cache (best-effort)."""
    tid = int(result.get("target_id") or 0)
    if not tid:
        return
    ts = checked_at or datetime.now(timezone.utc).replace(tzinfo=None)
    row = (
        db.query(AccountTargetMembershipProbe)
        .filter(
            AccountTargetMembershipProbe.account_id == account_id,
            AccountTargetMembershipProbe.target_id == tid,
        )
        .first()
    )
    if row:
        row.status = str(result.get("status") or MEMBERSHIP_UNKNOWN)
        row.can_post = result.get("can_post")
        row.message = (result.get("message") or "")[:2000]
        row.checked_at = ts
    else:
        db.add(
            AccountTargetMembershipProbe(
                account_id=account_id,
                target_id=tid,
                status=str(result.get("status") or MEMBERSHIP_UNKNOWN),
                can_post=result.get("can_post"),
                message=(result.get("message") or "")[:2000],
                checked_at=ts,
            )
        )
