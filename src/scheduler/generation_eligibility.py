"""
P5B-R — Central scheduled-job generation eligibility (fail-closed).

Generation eligibility is separate from execution authorization. During
``AUTOSTORY_PRODUCTION_CERTIFIED_NO_GO``, normal background PROMO/INFO generation
must not insert executable PENDING rows.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Optional

import structlog
from sqlalchemy.orm import Session

from config.settings import settings
from src.core.account_operational_state import compute_account_operational_state
from src.core.models import Account, AccountStatus
from src.core.scheduler_models import (
    AccountTargetBinding,
    ChatTarget,
    SCHEDULED_JOB_P4C_CERTIFICATION_MARKER,
    SCHEDULED_JOB_P5A_CERTIFICATION_MARKER,
    SCHEDULED_JOB_P5C_CERTIFICATION_MARKER,
    SCHEDULED_JOB_P5D_CERTIFICATION_MARKER,
    MessageType,
)
from src.clients.target_health import (
    classify_target,
    HEALTH_INVALID,
    is_health_allowed_for_send,
    merged_target_health_row,
)

logger = structlog.get_logger(__name__)

POLICY_VERSION = "p5br-v1"

SCOPED_CERTIFICATION_MARKERS = frozenset(
    {
        SCHEDULED_JOB_P4C_CERTIFICATION_MARKER,
        SCHEDULED_JOB_P5A_CERTIFICATION_MARKER,
        SCHEDULED_JOB_P5C_CERTIFICATION_MARKER,
        SCHEDULED_JOB_P5D_CERTIFICATION_MARKER,
    }
)

SEND_CAPABLE_JOB_TYPES = frozenset(
    {
        MessageType.PROMO.value,
        MessageType.INFO.value,
        "PROMO",
        "INFO",
    }
)

VALID_PROMO_GENERATION_MODES = frozenset({"disabled", "planning_only", "executable"})


@dataclass
class GenerationEligibilityDecision:
    allowed: bool
    reason_code: str
    human_reason: str
    policy_version: str = POLICY_VERSION
    evaluated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    audit: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason_code": self.reason_code,
            "human_reason": self.human_reason,
            "policy_version": self.policy_version,
            "evaluated_at": self.evaluated_at,
            **self.audit,
        }


def _deny(
    reason_code: str,
    human_reason: str,
    *,
    audit: dict[str, Any] | None = None,
) -> GenerationEligibilityDecision:
    return GenerationEligibilityDecision(
        allowed=False,
        reason_code=reason_code,
        human_reason=human_reason,
        audit=dict(audit or {}),
    )


def _allow(*, audit: dict[str, Any] | None = None) -> GenerationEligibilityDecision:
    return GenerationEligibilityDecision(
        allowed=True,
        reason_code="generation_allowed",
        human_reason="Generation permitted for scoped certification path.",
        audit=dict(audit or {}),
    )


def production_certified_no_go_active() -> bool:
    raw = (os.environ.get("AUTOSTORY_PRODUCTION_CERTIFIED_NO_GO") or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    try:
        return bool(getattr(settings, "production_certified_no_go", True))
    except Exception:
        return True


def promo_generation_mode() -> str:
    raw = (os.environ.get("PROMO_GENERATION_MODE") or "").strip().lower()
    if raw:
        return raw
    try:
        return (getattr(settings, "promo_generation_mode", "disabled") or "disabled").strip().lower()
    except Exception:
        return "disabled"


def _evaluate_global_generation_mode(
    job_type: str,
    *,
    generation_scope: str,
    generator_date: Optional[date],
) -> GenerationEligibilityDecision | None:
    """Return deny decision when global mode blocks; None when checks should continue."""
    jt = (job_type or "").strip().upper()
    if jt not in SEND_CAPABLE_JOB_TYPES:
        return _deny(
            "job_type_not_enabled",
            f"Job type {job_type!r} is not enabled for background generation.",
            audit={"job_type": jt, "generation_scope": generation_scope},
        )

    scope = (generation_scope or "normal").strip().lower()
    if scope in ("p4c_certification", "p5a_certification", "p5c_certification"):
        return None

    mode = promo_generation_mode()
    if not mode:
        return _deny(
            "generation_disabled",
            "PROMO_GENERATION_MODE unset; generation denied by default.",
            audit={"promo_generation_mode": mode or None, "generator_date": str(generator_date)},
        )
    if mode not in VALID_PROMO_GENERATION_MODES:
        return _deny(
            "unknown_generation_mode",
            f"Unknown PROMO_GENERATION_MODE={mode!r}; generation denied.",
            audit={"promo_generation_mode": mode, "generator_date": str(generator_date)},
        )
    if mode == "disabled":
        return _deny(
            "generation_disabled",
            "PROMO_GENERATION_MODE=disabled; background generation denied.",
            audit={"promo_generation_mode": mode, "generator_date": str(generator_date)},
        )
    if mode == "planning_only":
        return _deny(
            "generation_disabled",
            "PROMO_GENERATION_MODE=planning_only; executable generation denied.",
            audit={"promo_generation_mode": mode, "generator_date": str(generator_date)},
        )

    if production_certified_no_go_active():
        return _deny(
            "production_no_go",
            "AUTOSTORY_PRODUCTION_CERTIFIED_NO_GO active; normal generation denied.",
            audit={
                "production_certified_no_go": True,
                "promo_generation_mode": mode,
                "generator_date": str(generator_date),
            },
        )

    return None


def _readiness_stale(checked_at_iso: Optional[str], *, max_age_hours: int = 48) -> bool:
    if not checked_at_iso:
        return True
    try:
        ts = checked_at_iso.replace("Z", "+00:00")
        checked = datetime.fromisoformat(ts)
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - checked.astimezone(timezone.utc)
        return age.total_seconds() > max_age_hours * 3600
    except (ValueError, TypeError):
        return True


def evaluate_generation_eligibility(
    db: Session,
    *,
    job_type: str,
    account_id: int,
    target_id: int,
    generation_scope: str = "normal",
    generator_date: Optional[date] = None,
    job_marker: Optional[str] = None,
    schedule_rule_id: Optional[int] = None,
    schedule_profile_id: Optional[int] = None,
) -> GenerationEligibilityDecision:
    """
    Fail-closed eligibility for inserting a send-capable scheduled job.
    """
    audit_base: dict[str, Any] = {
        "job_type": (job_type or "").strip().upper(),
        "account_id": int(account_id),
        "target_id": int(target_id),
        "generation_scope": generation_scope,
        "generator_date": str(generator_date) if generator_date else None,
        "schedule_rule_id": schedule_rule_id,
        "schedule_profile_id": schedule_profile_id,
        "job_marker": job_marker,
    }

    marker = (job_marker or "").strip()
    scope = (generation_scope or "normal").strip().lower()

    if marker in SCOPED_CERTIFICATION_MARKERS and scope.endswith("_certification"):
        return _allow(audit={**audit_base, "scoped_certification": True})

    if marker in SCOPED_CERTIFICATION_MARKERS and scope == "normal":
        return _deny(
            "scope_not_authorized",
            "Certification marker present but generation_scope is not scoped.",
            audit=audit_base,
        )

    global_block = _evaluate_global_generation_mode(
        job_type,
        generation_scope=scope,
        generator_date=generator_date,
    )
    if global_block is not None:
        global_block.audit.update(audit_base)
        return global_block

    account = db.query(Account).filter(Account.id == int(account_id)).first()
    if account is None:
        return _deny("account_missing", "Account does not exist.", audit=audit_base)

    status_val = getattr(account.status, "value", str(account.status or "")).lower()
    if status_val != AccountStatus.ACTIVE.value:
        return _deny(
            "account_disabled",
            f"Account status {status_val!r} is not active.",
            audit=audit_base,
        )

    op = compute_account_operational_state(db, account)
    tier = (op.get("tier") or "").lower()
    if tier in ("reserved", "controller"):
        return _deny(
            "account_quarantined",
            f"Account tier {tier!r} is not eligible for normal generation.",
            audit={**audit_base, "tier": tier},
        )

    readiness_status = (op.get("readiness_status") or "").strip().upper()
    if readiness_status == "NOT_AUTHORIZED":
        return _deny(
            "account_not_authorized",
            "Account readiness is NOT_AUTHORIZED.",
            audit={**audit_base, "readiness_status": readiness_status},
        )
    if readiness_status is None or readiness_status == "":
        return _deny(
            "account_readiness_missing",
            "No readiness snapshot for account.",
            audit=audit_base,
        )
    if readiness_status != "READY":
        return _deny(
            "account_not_operational",
            f"Account readiness {readiness_status!r} blocks generation.",
            audit={**audit_base, "readiness_status": readiness_status},
        )

    if _readiness_stale(op.get("last_resolved_at")):
        return _deny(
            "account_readiness_stale",
            "Account readiness snapshot is stale.",
            audit={**audit_base, "last_resolved_at": op.get("last_resolved_at")},
        )

    if op.get("resolver_code"):
        return _deny(
            "account_not_operational",
            f"Session resolver blocks account: {op.get('resolver_code')}.",
            audit={**audit_base, "resolver_code": op.get("resolver_code")},
        )

    if not op.get("scheduler_eligible"):
        return _deny(
            "account_not_operational",
            "Account is not scheduler-eligible.",
            audit={**audit_base, "warnings": op.get("warnings")},
        )

    target = db.query(ChatTarget).filter(ChatTarget.id == int(target_id)).first()
    if target is None:
        return _deny("target_missing", "Target does not exist.", audit=audit_base)

    if classify_target(target).get("health") == HEALTH_INVALID:
        return _deny(
            "target_disabled",
            "Target health is invalid.",
            audit=audit_base,
        )

    mh = merged_target_health_row(db, target, int(account_id))
    if not is_health_allowed_for_send(mh["health"]):
        return _deny(
            "target_disabled",
            f"Target health {mh['health']!r} blocks send-capable generation.",
            audit={**audit_base, "target_health": mh["health"]},
        )

    if not getattr(target, "tg_id", None):
        return _deny(
            "target_identity_unresolved",
            "Target Telegram identity is unresolved.",
            audit=audit_base,
        )

    binding = (
        db.query(AccountTargetBinding)
        .filter(
            AccountTargetBinding.account_id == int(account_id),
            AccountTargetBinding.target_id == int(target_id),
        )
        .first()
    )
    if binding is None:
        return _deny(
            "binding_missing",
            "No account-target binding exists.",
            audit=audit_base,
        )
    if not bool(getattr(binding, "can_post", False)):
        return _deny(
            "binding_cannot_post",
            "Binding can_post=false blocks generation.",
            audit=audit_base,
        )

    allowed_types = (binding.allowed_types or "PROMO,INFO").replace(" ", "").split(",")
    jt = audit_base["job_type"]
    if jt and jt not in allowed_types:
        return _deny(
            "scope_not_authorized",
            f"Job type {jt} not in binding allowed_types.",
            audit={**audit_base, "allowed_types": allowed_types},
        )

    return GenerationEligibilityDecision(
        allowed=True,
        reason_code="generation_allowed",
        human_reason="Account, target, and binding eligible (executable mode only).",
        audit=audit_base,
    )


def log_generation_denied(
    decision: GenerationEligibilityDecision,
    *,
    generator_date: Optional[date] = None,
) -> None:
    audit = dict(decision.audit)
    audit.pop("generator_date", None)
    logger.info(
        "scheduled_job_generation_denied",
        audit_event="scheduled_job_generation_denied",
        policy_version=decision.policy_version,
        reason=decision.reason_code,
        human_reason=decision.human_reason,
        generator_date=str(generator_date) if generator_date else None,
        **audit,
    )


def log_generation_allowed(
    *,
    job_type: str,
    account_id: int,
    target_id: int,
    generator_date: Optional[date] = None,
    job_id: Optional[int] = None,
) -> None:
    logger.info(
        "scheduled_job_generation_allowed",
        audit_event="scheduled_job_generation_allowed",
        policy_version=POLICY_VERSION,
        job_type=job_type,
        account_id=account_id,
        target_id=target_id,
        generator_date=str(generator_date) if generator_date else None,
        job_id=job_id,
    )
