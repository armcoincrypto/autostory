"""Canonical owner direct-message service (Send Now foundation).

Dry-run never calls Telegram send. Live send requires MESSAGES_EXECUTION_ENABLED.
At-most-once retry via unique idempotency_key + atomic SENDING claim.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.core.execution_guard import (
    ACTION_OWNER_DM_SEND,
    require_execution_allowed,
)
from src.messaging.eligibility import evaluate_dm_account_eligibility
from src.messaging.flags import messages_execution_enabled
from src.messaging.models import (
    STATUS_CREATED,
    STATUS_FAILED,
    STATUS_SENDING,
    STATUS_SENT,
    STATUS_UNCERTAIN,
    OwnerDmIntent,
)
from src.messaging.rate_policy import evaluate_owner_dm_rate_policy
from src.messaging.transport import CountingFakeTransport, DmTransport, TelegramDmTransport

logger = structlog.get_logger(__name__)

TELEGRAM_MAX_MESSAGE_LENGTH = 4096

SUPPORTED_PEER_TYPES = frozenset({"private", "bot", "user"})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _message_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _preview(text: str, n: int = 120) -> str:
    t = (text or "").strip().replace("\n", " ")
    return t if len(t) <= n else t[: n - 1] + "…"


def validate_dm_message(text: str) -> tuple[bool, str, str]:
    raw = text if text is not None else ""
    if not str(raw).strip():
        return False, "EMPTY_MESSAGE", "Message text is empty."
    if len(str(raw)) > TELEGRAM_MAX_MESSAGE_LENGTH:
        return (
            False,
            "MESSAGE_TOO_LONG",
            f"Message exceeds Telegram max length ({TELEGRAM_MAX_MESSAGE_LENGTH}).",
        )
    return True, "OK", "ok"


def validate_peer(peer_id: str, peer_type: str = "private") -> tuple[bool, str, str]:
    pid = (peer_id or "").strip()
    pt = (peer_type or "private").strip().lower()
    if not pid:
        return False, "PEER_INVALID", "Recipient is required."
    if pt not in SUPPORTED_PEER_TYPES and pt not in {"group", "supergroup", "channel"}:
        return False, "PEER_INVALID", f"Unsupported peer type: {pt}."
    if pt not in SUPPORTED_PEER_TYPES:
        return False, "PEER_INVALID", "Owner DMs support private/bot recipients only."
    return True, "OK", "ok"


# Owner-facing denial codes (Wave 7D). MESSAGES_DISABLED only for kill switch.
_OWNER_ELIGIBILITY_MESSAGES = {
    "PROTECTED": (
        "ACCOUNT_PROTECTED",
        "This account is protected and cannot be used for Messages.",
    ),
    "RESERVED": (
        "ACCOUNT_RESERVED",
        "This account is reserved for another workflow.",
    ),
    "DISABLED": (
        "ACCOUNT_DISABLED",
        "This account is disabled and cannot be used for Messages.",
    ),
    "AUTH_FAILED": (
        "AUTH_REQUIRED",
        "This account needs authentication before sending.",
    ),
    "NEEDS_SESSION": (
        "AUTH_REQUIRED",
        "This account needs authentication before sending.",
    ),
}


def owner_message_for_eligibility(code: str, fallback_reason: str = "") -> tuple[str, str]:
    """Map eligibility.code → (owner error_code, owner message)."""
    mapped = _OWNER_ELIGIBILITY_MESSAGES.get((code or "").strip().upper())
    if mapped:
        return mapped
    return (
        "ACCOUNT_INELIGIBLE",
        (fallback_reason or "This account cannot be used for Messages.").strip(),
    )


def owner_message_for_guard_deny(blocked: Any) -> tuple[str, str]:
    """Map execution-guard DENY → owner codes. Kill switch only → MESSAGES_DISABLED."""
    reason = str(getattr(blocked, "reason_code", "") or "")
    blockers = {str(b) for b in (getattr(blocked, "blockers", None) or [])}
    if reason == "messages_execution_disabled":
        return "MESSAGES_DISABLED", "Message sending is currently disabled."
    if "account_protected" in blockers:
        return (
            "ACCOUNT_PROTECTED",
            "This account is protected and cannot be used for Messages.",
        )
    if "account_ai_reserved" in blockers:
        return (
            "ACCOUNT_RESERVED",
            "This account is reserved for another workflow.",
        )
    if "account_held" in blockers:
        return (
            "ACCOUNT_DISABLED",
            "This account is on hold and cannot be used for Messages.",
        )
    detail = str(getattr(blocked, "message", "") or reason or "Send denied.")
    return "ACCOUNT_INELIGIBLE", detail


class OwnerDirectMessageService:
    def __init__(self, transport: Optional[DmTransport] = None) -> None:
        self.transport: DmTransport = transport or TelegramDmTransport()

    def dry_run(
        self,
        db: Session,
        *,
        account_id: int,
        peer_id: str,
        text: str,
        peer_type: str = "private",
    ) -> dict[str, Any]:
        """Non-mutating validation path. Never calls transport.send_message_async."""
        aid = int(account_id)
        ok_msg, msg_code, msg_reason = validate_dm_message(text)
        ok_peer, peer_code, peer_reason = validate_peer(peer_id, peer_type)
        eligibility = evaluate_dm_account_eligibility(db, aid)
        rate = evaluate_owner_dm_rate_policy(db, aid)

        enabled = messages_execution_enabled()
        guard_result: dict[str, Any] = {
            "messages_execution_enabled": enabled,
            "action": ACTION_OWNER_DM_SEND,
            "would_block_live_send": not enabled,
        }

        would_send = (
            ok_msg
            and ok_peer
            and eligibility.eligible
            and bool(rate.get("allowed"))
            and enabled
        )
        reason_parts = []
        if not ok_msg:
            reason_parts.append(msg_reason)
        if not ok_peer:
            reason_parts.append(peer_reason)
        if not eligibility.eligible:
            reason_parts.append(eligibility.reason)
        if not rate.get("allowed"):
            reason_parts.append(str(rate.get("reason") or "rate limited"))
        if not enabled:
            reason_parts.append("MESSAGES_EXECUTION_ENABLED=false")

        return {
            "dry_run": True,
            "would_send": would_send,
            "account_id": aid,
            "recipient_id": (peer_id or "").strip(),
            "recipient_type": (peer_type or "private").strip().lower(),
            "eligibility": eligibility.to_dict(),
            "guard_result": guard_result,
            "rate_limit_result": rate,
            "validation": {
                "message_ok": ok_msg,
                "message_code": msg_code,
                "peer_ok": ok_peer,
                "peer_code": peer_code,
            },
            "reason": "; ".join(reason_parts)
            if reason_parts
            else "Would send if execution enabled and claimed.",
            "transport_send_count": 0,
        }

    def _load_by_key(self, db: Session, key: str) -> Optional[OwnerDmIntent]:
        return (
            db.query(OwnerDmIntent)
            .filter(OwnerDmIntent.idempotency_key == key)
            .first()
        )

    def _intent_result(self, intent: OwnerDmIntent, *, replay: bool = False) -> dict[str, Any]:
        return {
            "ok": intent.status == STATUS_SENT,
            "replay": replay,
            "intent_id": intent.id,
            "idempotency_key": intent.idempotency_key,
            "account_id": intent.account_id,
            "peer_id": intent.peer_id,
            "peer_type": intent.peer_type,
            "status": intent.status,
            "telegram_message_id": intent.telegram_message_id,
            "error_code": intent.error_code,
            "error_message": intent.error_message,
            "sent_at": intent.sent_at.isoformat() + "Z" if intent.sent_at else None,
            "message_preview": intent.message_preview,
        }

    def claim_or_get_intent(
        self,
        db: Session,
        *,
        idempotency_key: str,
        account_id: int,
        peer_id: str,
        peer_type: str,
        text: str,
        claim_owner: Optional[str] = None,
    ) -> tuple[OwnerDmIntent, bool]:
        """Return (intent, should_send). Only the unique CREATED→SENDING claim may send."""
        key = (idempotency_key or "").strip()
        if not key:
            raise ValueError("idempotency_key is required")

        existing = self._load_by_key(db, key)
        if existing is not None:
            return existing, False

        intent = OwnerDmIntent(
            idempotency_key=key,
            account_id=int(account_id),
            peer_id=(peer_id or "").strip(),
            peer_type=(peer_type or "private").strip().lower(),
            message_hash=_message_hash(text),
            message_preview=_preview(text),
            status=STATUS_CREATED,
            claim_owner=None,
        )
        db.add(intent)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            existing = self._load_by_key(db, key)
            if existing is None:
                raise
            return existing, False

        owner = claim_owner or f"claim-{uuid.uuid4().hex[:12]}"
        rows = (
            db.query(OwnerDmIntent)
            .filter(
                OwnerDmIntent.id == intent.id,
                OwnerDmIntent.status == STATUS_CREATED,
            )
            .update(
                {
                    OwnerDmIntent.status: STATUS_SENDING,
                    OwnerDmIntent.claim_owner: owner,
                    OwnerDmIntent.attempt_started_at: _utcnow(),
                    OwnerDmIntent.updated_at: _utcnow(),
                },
                synchronize_session=False,
            )
        )
        db.flush()
        db.refresh(intent)
        if rows != 1 or intent.status != STATUS_SENDING:
            return intent, False
        return intent, True

    async def send_now(
        self,
        db: Session,
        *,
        account_id: int,
        peer_id: str,
        text: str,
        idempotency_key: str,
        peer_type: str = "private",
        claim_owner: Optional[str] = None,
        _force_post_send_db_failure: bool = False,
    ) -> dict[str, Any]:
        """Live owner DM send with idempotency. Requires MESSAGES_EXECUTION_ENABLED."""
        # Idempotent replay first — never re-evaluate rate/guard for an existing key.
        existing = self._load_by_key(db, (idempotency_key or "").strip())
        if existing is not None:
            return self._intent_result(existing, replay=True)

        ok_msg, msg_code, msg_reason = validate_dm_message(text)
        if not ok_msg:
            return {
                "ok": False,
                "status": "FAILED",
                "error_code": msg_code,
                "error_message": msg_reason,
                "idempotency_key": idempotency_key,
            }
        ok_peer, peer_code, peer_reason = validate_peer(peer_id, peer_type)
        if not ok_peer:
            return {
                "ok": False,
                "status": "FAILED",
                "error_code": peer_code,
                "error_message": peer_reason,
                "idempotency_key": idempotency_key,
            }

        # Eligibility is the product policy owner (Wave 7D: clear codes before guard).
        eligibility = evaluate_dm_account_eligibility(db, int(account_id))
        if not eligibility.eligible:
            err_code, err_msg = owner_message_for_eligibility(
                eligibility.code, eligibility.reason
            )
            return {
                "ok": False,
                "status": "FAILED",
                "error_code": err_code,
                "error_message": err_msg,
                "eligibility": eligibility.to_dict(),
                "idempotency_key": idempotency_key,
            }

        blocked = require_execution_allowed(
            ACTION_OWNER_DM_SEND, account_id=int(account_id)
        )
        if blocked is not None:
            err_code, err_msg = owner_message_for_guard_deny(blocked)
            return {
                "ok": False,
                "status": "DENIED",
                "error_code": err_code,
                "error_message": err_msg,
                "idempotency_key": idempotency_key,
            }

        rate = evaluate_owner_dm_rate_policy(db, int(account_id))
        if not rate.get("allowed"):
            return {
                "ok": False,
                "status": "FAILED",
                "error_code": "RATE_LIMITED",
                "error_message": rate.get("reason"),
                "retry_after": rate.get("retry_after"),
                "idempotency_key": idempotency_key,
            }

        intent, should_send = self.claim_or_get_intent(
            db,
            idempotency_key=idempotency_key,
            account_id=account_id,
            peer_id=peer_id,
            peer_type=peer_type,
            text=text,
            claim_owner=claim_owner,
        )
        db.commit()

        if not should_send:
            db.refresh(intent)
            return self._intent_result(intent, replay=True)

        try:
            result = await self.transport.send_message_async(
                int(account_id), (peer_id or "").strip(), text
            )
        except Exception as e:
            logger.exception("owner_dm_transport_raised", intent_id=intent.id)
            intent.status = STATUS_FAILED
            intent.error_code = "UNKNOWN"
            intent.error_message = str(e)[:240]
            intent.updated_at = _utcnow()
            db.add(intent)
            db.commit()
            return self._intent_result(intent)

        if not result.get("ok") and not result.get("success"):
            intent.status = STATUS_FAILED
            intent.error_code = result.get("error_code") or "UNKNOWN"
            intent.error_message = (result.get("error_message") or "")[:240]
            intent.updated_at = _utcnow()
            db.add(intent)
            db.commit()
            return self._intent_result(intent)

        mid = result.get("telegram_message_id")
        sent_at = result.get("sent_at") or _utcnow()
        try:
            if _force_post_send_db_failure:
                raise RuntimeError("simulated_post_send_db_failure")
            intent.status = STATUS_SENT
            intent.telegram_message_id = int(mid) if mid is not None else None
            intent.sent_at = sent_at if isinstance(sent_at, datetime) else _utcnow()
            intent.error_code = None
            intent.error_message = None
            intent.updated_at = _utcnow()
            db.add(intent)
            db.commit()
            return self._intent_result(intent)
        except Exception as e:
            logger.error(
                "owner_dm_post_send_persist_failed",
                intent_id=intent.id,
                telegram_message_id=mid,
                error=str(e),
            )
            try:
                db.rollback()
                intent2 = self._load_by_key(db, intent.idempotency_key)
                if intent2 is not None:
                    intent2.status = STATUS_UNCERTAIN
                    if mid is not None:
                        intent2.telegram_message_id = int(mid)
                    intent2.error_code = "UNCERTAIN"
                    intent2.error_message = (
                        "Telegram may have accepted the message but delivery "
                        "confirmation could not be persisted. Do not retry same key."
                    )
                    intent2.updated_at = _utcnow()
                    db.add(intent2)
                    db.commit()
                    return self._intent_result(intent2)
            except Exception:
                logger.exception(
                    "owner_dm_uncertain_persist_failed", intent_id=intent.id
                )
            return {
                "ok": False,
                "status": STATUS_UNCERTAIN,
                "idempotency_key": intent.idempotency_key,
                "intent_id": intent.id,
                "telegram_message_id": mid,
                "error_code": "UNCERTAIN",
                "error_message": "Post-send persistence failed; do not retry.",
                "replay": False,
            }


def fetch_recent_messages(
    transport: DmTransport,
    account_id: int,
    peer: str,
    limit: int = 20,
):
    """Generic history primitive (policy-free)."""
    return transport.fetch_recent_messages_async(int(account_id), peer, limit)
