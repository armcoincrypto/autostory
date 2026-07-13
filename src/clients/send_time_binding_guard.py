"""
Send-time binding enforcement for gateway / execution_guard.

Reuses ``classify_binding_verification`` — no second binding engine.
Preferred TOCTOU policy: persisted VERIFIED_CAN_POST + fresh TTL (default 3600s).
No live Telegram probe at send time.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session

from src.clients.binding_verification import (
    BINDING_VERIFICATION_FRESH_TTL_SEC,
    VERIFIED_CAN_POST,
    classify_binding_verification,
)


def evaluate_send_time_binding_guard(
    db: Session,
    *,
    account_id: int,
    target_id: int,
    binding_id: Optional[int] = None,
) -> tuple[bool, str, dict[str, Any]]:
    """
    Returns (ok, reason_code, audit).
    Fail-closed unless production_verified VERIFIED_CAN_POST.
    """
    audit: dict[str, Any] = {
        "account_id": int(account_id),
        "target_id": int(target_id),
        "binding_id": int(binding_id) if binding_id is not None else None,
        "binding_fresh_ttl_sec": BINDING_VERIFICATION_FRESH_TTL_SEC,
    }

    if binding_id is not None:
        from src.core.scheduler_models import AccountTargetBinding

        b = (
            db.query(AccountTargetBinding)
            .filter(AccountTargetBinding.id == int(binding_id))
            .first()
        )
        if b is None:
            return False, "binding_missing", audit
        if int(b.account_id) != int(account_id):
            return False, "binding_account_mismatch", {**audit, "binding_account_id": int(b.account_id)}
        if int(b.target_id) != int(target_id):
            return False, "binding_target_mismatch", {**audit, "binding_target_id": int(b.target_id)}

    ver = classify_binding_verification(db, int(account_id), int(target_id))
    audit["binding_verification"] = ver.get("status")
    audit["production_verified"] = bool(ver.get("production_verified"))
    audit["membership_status"] = ver.get("membership_status")
    audit["permission_checked_at"] = ver.get("permission_checked_at")

    if not ver.get("production_verified") or ver.get("status") != VERIFIED_CAN_POST:
        return False, str(ver.get("reason_code") or "binding_not_verified"), {
            **audit,
            "human_reason": ver.get("human_reason"),
        }
    return True, "send_time_binding_ok", audit
