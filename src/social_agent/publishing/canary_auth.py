"""Short-lived single-use canary authorization objects.

Authorization is a server-minted DB row + secret. Callers cannot authorize
with a boolean flag.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from src.social_agent.models import SocialPublishCanaryAuthorization
from src.social_agent.publishing.execution import (
    CANARY_AUTH_TTL_SECONDS,
    CANARY_DESTINATION,
    CANARY_FACEBOOK_PAGE_ID,
    PublishingExecutionMode,
)


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CanaryAuthorization:
    """In-memory authorization presented to the Meta provider adapter."""

    authorization_id: int
    authorization_secret: str
    actor: str
    workspace_id: str
    page_id: str
    dry_run_id: int
    payload_hash: str
    idempotency_key: str
    destination: str
    execution_mode: str
    expires_at: datetime

    def fingerprint(self) -> str:
        return _hash_secret(self.authorization_secret)


def mint_canary_authorization(
    db: Session,
    *,
    actor: str,
    workspace_id: str,
    page_id: str,
    dry_run_id: int,
    payload_hash: str,
    idempotency_key: str,
    explicit_approval: str,
    ttl_seconds: int = CANARY_AUTH_TTL_SECONDS,
) -> dict[str, Any]:
    """Mint a single-use canary authorization. Requires explicit_approval == 'CONFIRM'."""
    if explicit_approval != "CONFIRM":
        return {
            "ok": False,
            "error": "explicit_approval_required",
            "message": "Canary authorization requires explicit_approval='CONFIRM' (not a boolean).",
        }
    if str(page_id) != CANARY_FACEBOOK_PAGE_ID:
        return {
            "ok": False,
            "error": "wrong_page",
            "message": f"Canary only allows Page {CANARY_FACEBOOK_PAGE_ID}.",
        }
    if not actor or not workspace_id or not payload_hash or not idempotency_key:
        return {"ok": False, "error": "missing_required_fields"}
    if not dry_run_id:
        return {"ok": False, "error": "dry_run_id_required"}

    secret = secrets.token_urlsafe(32)
    row = SocialPublishCanaryAuthorization(
        actor=actor,
        workspace_id=workspace_id,
        page_id=str(page_id),
        dry_run_id=int(dry_run_id),
        payload_hash=str(payload_hash),
        idempotency_key=str(idempotency_key),
        destination=CANARY_DESTINATION,
        execution_mode=PublishingExecutionMode.CONTROLLED_CANARY.value,
        secret_hash=_hash_secret(secret),
        status="ACTIVE",
        expires_at=datetime.utcnow() + timedelta(seconds=int(ttl_seconds)),
        explicit_approval="CONFIRM",
    )
    db.add(row)
    db.flush()
    return {
        "ok": True,
        "authorization_id": row.id,
        "authorization_secret": secret,  # shown once to operator; never logged by callers
        "expires_at": row.expires_at.isoformat() + "Z",
        "page_id": row.page_id,
        "dry_run_id": row.dry_run_id,
        "payload_hash": row.payload_hash,
        "idempotency_key": row.idempotency_key,
        "execution_mode": row.execution_mode,
        "destination": row.destination,
        "single_use": True,
    }


def load_canary_authorization(
    db: Session,
    *,
    authorization_id: int,
    authorization_secret: str,
) -> tuple[CanaryAuthorization | None, str | None]:
    row = (
        db.query(SocialPublishCanaryAuthorization)
        .filter(SocialPublishCanaryAuthorization.id == int(authorization_id))
        .first()
    )
    if row is None:
        return None, "authorization_not_found"
    if row.status != "ACTIVE":
        return None, f"authorization_{row.status.lower()}"
    if row.expires_at < datetime.utcnow():
        row.status = "EXPIRED"
        db.flush()
        return None, "authorization_expired"
    if row.used_at is not None:
        return None, "authorization_reused"
    if row.secret_hash != _hash_secret(authorization_secret):
        return None, "authorization_secret_invalid"
    if row.page_id != CANARY_FACEBOOK_PAGE_ID:
        return None, "wrong_page"
    if row.destination != CANARY_DESTINATION:
        return None, "wrong_destination"
    if row.execution_mode != PublishingExecutionMode.CONTROLLED_CANARY.value:
        return None, "wrong_execution_mode"
    if row.explicit_approval != "CONFIRM":
        return None, "approval_missing"
    auth = CanaryAuthorization(
        authorization_id=row.id,
        authorization_secret=authorization_secret,
        actor=row.actor,
        workspace_id=row.workspace_id,
        page_id=row.page_id,
        dry_run_id=row.dry_run_id,
        payload_hash=row.payload_hash,
        idempotency_key=row.idempotency_key,
        destination=row.destination,
        execution_mode=row.execution_mode,
        expires_at=row.expires_at,
    )
    return auth, None


def consume_canary_authorization(
    db: Session,
    *,
    authorization_id: int,
    result_status: str,
    external_post_id: str | None = None,
) -> None:
    row = (
        db.query(SocialPublishCanaryAuthorization)
        .filter(SocialPublishCanaryAuthorization.id == int(authorization_id))
        .first()
    )
    if row is None:
        return
    row.used_at = datetime.utcnow()
    row.status = "USED"
    row.result_status = result_status
    row.external_post_id = external_post_id
    db.flush()


def disable_canary_authorization(db: Session, *, authorization_id: int, reason: str = "disabled") -> None:
    row = (
        db.query(SocialPublishCanaryAuthorization)
        .filter(SocialPublishCanaryAuthorization.id == int(authorization_id))
        .first()
    )
    if row is None:
        return
    if row.status == "ACTIVE":
        row.status = "DISABLED"
    row.result_status = reason
    if row.used_at is None:
        row.used_at = datetime.utcnow()
    db.flush()
