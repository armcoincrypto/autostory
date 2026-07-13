"""
Canonical binding verification for production eligibility.

Distinguishes configured ``can_post=True`` from Telegram-verified permission.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from src.clients import readiness_store
from src.clients.membership_check import (
    MEMBERSHIP_JOINED,
    MEMBERSHIP_NOT_JOINED,
    MEMBERSHIP_NO_PERMISSION_TO_POST,
    MEMBERSHIP_UNRESOLVED_TARGET,
)
from src.core.account_operational_state import compute_account_operational_state
from src.core.scheduler_models import (
    AccountTargetBinding,
    AccountTargetMembershipProbe,
    ChatTarget,
)

# Production-verified binding TTL (seconds). Stale April/May probes do not qualify.
BINDING_VERIFICATION_FRESH_TTL_SEC = int(
    os.environ.get("BINDING_VERIFICATION_FRESH_TTL_SEC", "3600")
)

VERIFIED_CAN_POST = "VERIFIED_CAN_POST"
CONFIGURED_NOT_VERIFIED = "CONFIGURED_NOT_VERIFIED"
NOT_JOINED = "NOT_JOINED"
NO_PERMISSION_TO_POST = "NO_PERMISSION_TO_POST"
UNRESOLVED_TARGET = "UNRESOLVED_TARGET"
ACCOUNT_NOT_READY = "ACCOUNT_NOT_READY"
ACCOUNT_NOT_AUTHORIZED = "ACCOUNT_NOT_AUTHORIZED"
QUARANTINED = "QUARANTINED"
TARGET_DISABLED = "TARGET_DISABLED"
STALE_CHECK = "STALE_CHECK"
PROBE_FAILED = "PROBE_FAILED"
PROBE_MISSING = "PROBE_MISSING"


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _probe_age_sec(probe: Optional[AccountTargetMembershipProbe], now: datetime) -> Optional[float]:
    if probe is None or probe.checked_at is None:
        return None
    ca = probe.checked_at
    if not isinstance(ca, datetime):
        return None
    return (now - ca).total_seconds()


def classify_binding_verification(
    db: Session,
    account_id: int,
    target_id: int,
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Return binding verification status without Telegram I/O."""
    from src.core.models import Account

    now = now or _utc_now_naive()
    aid, tid = int(account_id), int(target_id)

    binding = (
        db.query(AccountTargetBinding)
        .filter(AccountTargetBinding.account_id == aid, AccountTargetBinding.target_id == tid)
        .first()
    )
    account = db.query(Account).filter(Account.id == aid).first()
    target = db.query(ChatTarget).filter(ChatTarget.id == tid).first()
    probe = (
        db.query(AccountTargetMembershipProbe)
        .filter(
            AccountTargetMembershipProbe.account_id == aid,
            AccountTargetMembershipProbe.target_id == tid,
        )
        .order_by(AccountTargetMembershipProbe.checked_at.desc())
        .first()
    )

    if account is None or target is None:
        return {
            "status": PROBE_FAILED,
            "production_verified": False,
            "reason_code": "missing_account_or_target",
            "human_reason": "Account or target not found.",
        }
    if binding is None:
        return {
            "status": PROBE_MISSING,
            "production_verified": False,
            "reason_code": "binding_missing",
            "human_reason": "No account-target binding exists.",
        }

    op = compute_account_operational_state(db, account)
    if (op.get("tier") or "").lower() in ("reserved", "controller"):
        return {
            "status": QUARANTINED,
            "production_verified": False,
            "reason_code": "account_quarantined",
            "human_reason": "Account is quarantined.",
        }

    snap = readiness_store.fetch_snapshot(db, aid)
    if snap is None or snap.status != readiness_store.STAT_READY:
        st = snap.status if snap else "missing"
        return {
            "status": ACCOUNT_NOT_READY if st != readiness_store.STAT_NOT_AUTH else ACCOUNT_NOT_AUTHORIZED,
            "production_verified": False,
            "reason_code": "account_not_ready",
            "human_reason": f"Account readiness is {st}, not READY.",
        }
    if not readiness_store.snapshot_ready_and_valid(db, aid):
        return {
            "status": ACCOUNT_NOT_READY,
            "production_verified": False,
            "reason_code": "readiness_stale",
            "human_reason": "Account readiness is stale or expired.",
        }

    age = _probe_age_sec(probe, now)
    if probe is None or age is None:
        configured = bool(binding.can_post)
        return {
            "status": CONFIGURED_NOT_VERIFIED if configured else PROBE_MISSING,
            "production_verified": False,
            "reason_code": "binding_not_verified",
            "human_reason": (
                "Binding configured can_post=True but membership never verified."
                if configured
                else "Binding has no verified membership probe."
            ),
            "configured_can_post": configured,
            "membership_status": None,
            "permission_checked_at": None,
        }

    if age > float(BINDING_VERIFICATION_FRESH_TTL_SEC):
        return {
            "status": STALE_CHECK,
            "production_verified": False,
            "reason_code": "binding_check_stale",
            "human_reason": f"Last binding check is stale ({int(age)}s > {BINDING_VERIFICATION_FRESH_TTL_SEC}s).",
            "membership_status": probe.status,
            "permission_checked_at": probe.checked_at.isoformat() if probe.checked_at else None,
            "configured_can_post": bool(binding.can_post),
        }

    status = (probe.status or "").strip().lower()
    if status == MEMBERSHIP_UNRESOLVED_TARGET:
        return {
            "status": UNRESOLVED_TARGET,
            "production_verified": False,
            "reason_code": "unresolved_target",
            "human_reason": probe.message or "Target could not be resolved.",
            "membership_status": probe.status,
            "permission_checked_at": probe.checked_at.isoformat() if probe.checked_at else None,
        }
    if status == MEMBERSHIP_NOT_JOINED:
        return {
            "status": NOT_JOINED,
            "production_verified": False,
            "reason_code": "not_joined",
            "human_reason": probe.message or "Account is not a member of the target.",
            "membership_status": probe.status,
        }
    if status == MEMBERSHIP_NO_PERMISSION_TO_POST or probe.can_post is False:
        return {
            "status": NO_PERMISSION_TO_POST,
            "production_verified": False,
            "reason_code": "no_permission_to_post",
            "human_reason": probe.message or "Member but cannot post.",
            "membership_status": probe.status,
        }
    if status == MEMBERSHIP_JOINED and probe.can_post is True:
        return {
            "status": VERIFIED_CAN_POST,
            "production_verified": True,
            "reason_code": "verified_can_post",
            "human_reason": "Telegram-verified member with posting permission.",
            "membership_status": probe.status,
            "permission_checked_at": probe.checked_at.isoformat() if probe.checked_at else None,
            "configured_can_post": bool(binding.can_post),
        }

    return {
        "status": PROBE_FAILED,
        "production_verified": False,
        "reason_code": "binding_probe_inconclusive",
        "human_reason": probe.message or f"Probe status {probe.status!r} is not production-ready.",
        "membership_status": probe.status,
    }
