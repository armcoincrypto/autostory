"""
Story preflight check using Telegram CanSendStoryRequest.
Does NOT send a story; verifies eligibility before actual publish.
"""
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import structlog
from telethon import TelegramClient
from telethon.errors.rpcerrorlist import (
    FrozenMethodInvalidError,
    FrozenParticipantMissingError,
    UserRestrictedError,
)
from telethon.tl.functions.stories import CanSendStoryRequest
from telethon.tl.types import InputPeerSelf

logger = structlog.get_logger(__name__)

# Map Telegram RPC errors to our precheck status
PRECHECK_STATUS_ALLOWED = "allowed"
PRECHECK_STATUS_RATE_LIMITED = "rate_limited"
PRECHECK_STATUS_FROZEN = "frozen"
PRECHECK_STATUS_RESTRICTED = "restricted"
PRECHECK_STATUS_TELEGRAM_DENIED = "telegram_denied"
PRECHECK_STATUS_BLOCKED = "blocked"
PRECHECK_STATUS_FAILED_CHECK = "failed_check"
PRECHECK_STATUS_UNKNOWN = "unknown"


def _is_high_confidence_frozen_exception(exc: BaseException, err_lower: str) -> bool:
    """True when Telegram/Telethon clearly indicates a frozen-account class (not a weak substring hit)."""
    if isinstance(exc, (FrozenMethodInvalidError, FrozenParticipantMissingError)):
        return True
    return "not available for frozen accounts" in err_lower


def _is_high_confidence_restricted_exception(exc: BaseException) -> bool:
    return isinstance(exc, UserRestrictedError)


async def run_story_precheck(client: TelegramClient, account_id: int) -> Dict[str, Any]:
    """
    Call CanSendStoryRequest to check if account can post stories.
    Does not upload or send any media.

    Returns:
      {
        "status": "allowed" | "rate_limited" | "frozen" | "restricted" | "telegram_denied" | "blocked" | "failed_check" | "unknown",
        "reason": str,
        "retry_after_seconds": int | None,
        "checked_at": iso datetime,
      }
    """
    now = datetime.utcnow()
    result = {
        "status": PRECHECK_STATUS_UNKNOWN,
        "reason": "",
        "retry_after_seconds": None,
        "checked_at": now.isoformat() + "Z",
    }
    try:
        # Resolve peer: InputPeerSelf or get_input_entity("me") for robustness across Telethon versions
        peer = InputPeerSelf()
        try:
            me_entity = await client.get_input_entity("me")
            if me_entity is not None:
                peer = me_entity
        except Exception:
            pass
        await client(CanSendStoryRequest(peer=peer))
        result["status"] = PRECHECK_STATUS_ALLOWED
        result["reason"] = "CanSendStory OK"
        logger.info("story_precheck_allowed", account_id=account_id)
        return result
    except Exception as e:
        err_str = str(e)
        err_lower = err_str.lower()
        result["reason"] = err_str[:255]

        if "STORIES_TOO_MUCH" in err_str or "STORY_SEND_FLOOD" in err_str:
            result["status"] = PRECHECK_STATUS_RATE_LIMITED
            # Extract retry_after: exception attribute first, then regex. Never hardcode if we can extract.
            sec = None
            if hasattr(e, "seconds") and e.seconds is not None:
                sec = int(e.seconds)
            elif hasattr(e, "retry_after") and e.retry_after is not None:
                sec = int(e.retry_after)
            else:
                # Telethon's RPCError formats str(e) as "RPCError {code}: {message}
                # (caused by ...)" -- code is an HTTP-style status (e.g. 400) with no
                # wait-time meaning at all. Regex must run against the clean
                # `.message` (e.g. "STORIES_TOO_MUCH"), never the full formatted
                # string, or a plain digit scan will misread the status code itself
                # as a wait time (confirmed in production: "RPCError 400:
                # STORIES_TOO_MUCH" produced a bogus 400-second cooldown).
                search_text = str(getattr(e, "message", None) or err_str)
                import re
                for pattern in [
                    r"[Ww]ait\s+(\d+)\s*(?:s|sec|seconds?)?",
                    r"[Rr]etry[_\s]?after[=:\s]+(\d+)",
                    r"STORIES_TOO_MUCH(?:_|-)?(\d+)",
                    r"STORY_SEND_FLOOD(?:_|-)?(\d+)",
                    r"(\d+)\s*(?:s|sec|seconds?)\s*(?:to\s+)?(?:retry|wait)",
                    # Deliberately no generic "any N-digit number" fallback here --
                    # Telegram's STORIES_TOO_MUCH message carries no reset time, and
                    # guessing from an unrelated digit sequence is worse than the
                    # explicit 24h fallback below.
                ]:
                    m = re.search(pattern, search_text)
                    if m:
                        v = int(m.group(1))
                        if 60 <= v <= 86400 * 32:  # 1min to 32 days
                            sec = v
                            break
                        elif v < 60 and v >= 10:  # 10-59 sec
                            sec = v
                            break
            if sec is not None:
                result["retry_after_seconds"] = min(sec, 86400 * 32)
            else:
                result["retry_after_seconds"] = 86400  # fallback only when extraction fails
            logger.info(
                "story_precheck_rate_limited",
                account_id=account_id,
                reason=err_str[:100],
                retry_after=result["retry_after_seconds"],
            )
        elif _is_high_confidence_frozen_exception(e, err_lower):
            result["status"] = PRECHECK_STATUS_FROZEN
            logger.warning("story_precheck_frozen", account_id=account_id, error=err_str[:100])
        elif _is_high_confidence_restricted_exception(e):
            result["status"] = PRECHECK_STATUS_RESTRICTED
            logger.warning("story_precheck_restricted", account_id=account_id, error=err_str[:100])
        elif "PEER_ID_INVALID" in err_str or "AUTH" in err_str:
            result["status"] = PRECHECK_STATUS_BLOCKED
            logger.warning("story_precheck_blocked", account_id=account_id, error=err_str[:100])
        elif "frozen" in err_lower or "not available for frozen" in err_lower:
            # Weak text-only signal — do not claim a definitive Telegram "frozen" classification.
            result["status"] = PRECHECK_STATUS_TELEGRAM_DENIED
            logger.warning("story_precheck_telegram_denied_frozen_like", account_id=account_id, error=err_str[:100])
        elif "restricted" in err_lower:
            result["status"] = PRECHECK_STATUS_TELEGRAM_DENIED
            logger.warning("story_precheck_telegram_denied_restricted_like", account_id=account_id, error=err_str[:100])
        else:
            result["status"] = PRECHECK_STATUS_FAILED_CHECK
            logger.warning("story_precheck_failed", account_id=account_id, error=err_str[:100])

        return result


def persist_precheck_result(
    account_id: int,
    status: str,
    reason: str,
    retry_after_seconds: Optional[int],
) -> None:
    """
    Persist precheck result to DB.

    When precheck returns ALLOWED: reconcile stale state by setting story_status='ok',
    clearing story_status_reason and story_blocked_until. Precheck is fresher truth.

    When rate_limited: set story_blocked_until from retry_after_seconds.
    """
    from src.core.database import get_db_context
    from src.core.models import Account

    now = datetime.utcnow()
    blocked_until = None
    if retry_after_seconds and status == PRECHECK_STATUS_RATE_LIMITED:
        blocked_until = now + timedelta(seconds=retry_after_seconds)

    with get_db_context() as db:
        acc = db.query(Account).filter(Account.id == account_id).first()
        if acc:
            acc.story_precheck_status = status
            acc.story_precheck_reason = (reason[:255] if reason else None)
            acc.story_precheck_checked_at = now

            if status == PRECHECK_STATUS_ALLOWED:
                # Reconcile: precheck allowed overrides stale story_status/blocked_until
                old_ss = getattr(acc, "story_status", None)
                old_sb = getattr(acc, "story_blocked_until", None)
                acc.story_status = "ok"
                acc.story_status_reason = None
                acc.story_blocked_until = None
                sb_str = old_sb.isoformat() if (old_sb and hasattr(old_sb, "isoformat")) else (str(old_sb) if old_sb else "null")
                if old_ss not in (None, "ok", "unknown") or old_sb:
                    logger.info(
                        "story_precheck_allowed_reconciled",
                        account_id=account_id,
                        old_story_status=old_ss,
                        old_blocked_until=sb_str,
                    )
            elif blocked_until is not None:
                acc.story_blocked_until = blocked_until

            db.commit()
            logger.info(
                "story_precheck_persisted",
                account_id=account_id,
                status=status,
                blocked_until=blocked_until.isoformat() if blocked_until else None,
            )
