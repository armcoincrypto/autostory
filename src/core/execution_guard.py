"""
P3 — Central execution permission guard (default DENY).

All live Telegram / story / campaign / discovery / mutation paths should call
``can_execute_action`` before side effects. Dry-run paths may proceed with audit only.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

import structlog

logger = structlog.get_logger(__name__)

ACTION_TELEGRAM_SEND = "telegram_send"
ACTION_TELEGRAM_JOIN = "telegram_join"
ACTION_STORY_PUBLISH = "story_publish"
ACTION_CAMPAIGN_EXECUTE = "campaign_execute"
ACTION_DISCOVERY_SCAN = "discovery_scan"
ACTION_DISCOVERY_JOIN = "discovery_join"
ACTION_ACCOUNT_MUTATION = "account_mutation"
ACTION_AI_CODING_EXECUTE = "ai_coding_execute"

ACTION_TYPES = frozenset(
    {
        ACTION_TELEGRAM_SEND,
        ACTION_TELEGRAM_JOIN,
        ACTION_STORY_PUBLISH,
        ACTION_CAMPAIGN_EXECUTE,
        ACTION_DISCOVERY_SCAN,
        ACTION_DISCOVERY_JOIN,
        ACTION_ACCOUNT_MUTATION,
        ACTION_AI_CODING_EXECUTE,
    }
)

RESULT_ALLOW = "ALLOW"
RESULT_DENY = "DENY"


@dataclass
class ExecutionGuardDecision:
    allowed: bool
    action: str
    result: str
    reason_code: str
    message: str
    blockers: list[str] = field(default_factory=list)
    audit: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "action": self.action,
            "result": self.result,
            "reason_code": self.reason_code,
            "message": self.message,
            "blockers": list(self.blockers),
            "audit": dict(self.audit),
        }


def _deny(
    action: str,
    reason_code: str,
    message: str,
    *,
    blockers: list[str] | None = None,
    audit: dict[str, Any] | None = None,
) -> ExecutionGuardDecision:
    return ExecutionGuardDecision(
        allowed=False,
        action=action,
        result=RESULT_DENY,
        reason_code=reason_code,
        message=message,
        blockers=list(blockers or [reason_code]),
        audit=dict(audit or {}),
    )


def _allow(
    action: str,
    reason_code: str,
    message: str,
    *,
    audit: dict[str, Any] | None = None,
) -> ExecutionGuardDecision:
    return ExecutionGuardDecision(
        allowed=True,
        action=action,
        result=RESULT_ALLOW,
        reason_code=reason_code,
        message=message,
        blockers=[],
        audit=dict(audit or {}),
    )


def execution_emergency_lock_active() -> bool:
    return os.environ.get("EXECUTION_EMERGENCY_LOCK", "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def discovery_execution_enabled() -> bool:
    try:
        from config.settings import settings

        return bool(getattr(settings, "discovery_execution_enabled", False))
    except Exception:
        return os.environ.get("DISCOVERY_EXECUTION_ENABLED", "false").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )


def _operator_approval_required() -> bool:
    return bool((os.environ.get("EXECUTION_OPERATOR_APPROVAL_TOKEN") or "").strip())


def _operator_approval_ok(token: str | None) -> bool:
    required = (os.environ.get("EXECUTION_OPERATOR_APPROVAL_TOKEN") or "").strip()
    if not required:
        return True
    return bool(token and token.strip() == required)


def _account_blocked(account_id: int | None) -> tuple[bool, list[str]]:
    if account_id is None:
        return False, []
    blockers: list[str] = []
    try:
        from src.recovery.p9_83_governance_observability import PROTECTED_IDS, PURPOSE_HOLD_IDS

        aid = int(account_id)
        if aid in PROTECTED_IDS:
            blockers.append("account_protected")
        if aid in PURPOSE_HOLD_IDS:
            blockers.append("account_held")
    except Exception:
        pass
    try:
        from src.ai_agent.account_allowlist import RESERVED_AI_AGENT_ACCOUNT_IDS

        if int(account_id) in RESERVED_AI_AGENT_ACCOUNT_IDS:
            blockers.append("account_ai_reserved")
    except Exception:
        pass
    return bool(blockers), blockers


def _account_readiness_blockers(db, account_id: int | None) -> list[str]:
    if db is None or account_id is None:
        return []
    blockers: list[str] = []
    try:
        from src.scheduler.runtime_preflight import classify_scheduler_runtime_gate
        from src.core.models import Account

        account = db.query(Account).filter(Account.id == int(account_id)).first()
        if not account:
            return ["account_not_found"]
        gate = classify_scheduler_runtime_gate(db, account)
        action = gate.get("action")
        if action == "skip_permanent":
            blockers.extend(gate.get("permanent_blockers") or [])
            if gate.get("primary_blocker"):
                blockers.append(str(gate["primary_blocker"]))
        elif action == "defer_temp_lock":
            blockers.extend(gate.get("temporary_blockers") or [])
    except Exception as exc:
        blockers.append(f"readiness_check_failed:{exc}")
    return blockers


def _log_guard_decision(decision: ExecutionGuardDecision, **ctx: Any) -> None:
    logger.info(
        "execution_guard_decision",
        action=decision.action,
        result=decision.result,
        reason_code=decision.reason_code,
        allowed=decision.allowed,
        blockers=decision.blockers[:8],
        **ctx,
    )
    try:
        from src.core.database import get_db_context
        from src.core.models import SystemLog

        with get_db_context() as db:
            db.add(
                SystemLog(
                    level="INFO" if decision.allowed else "WARNING",
                    component="execution_guard",
                    message=f"{decision.action}:{decision.result}",
                    details={
                        **decision.to_dict(),
                        **{k: v for k, v in ctx.items() if k not in ("db",)},
                        "ts": datetime.utcnow().isoformat() + "Z",
                    },
                )
            )
    except Exception:
        pass


def can_execute_action(
    action_type: str,
    *,
    account_id: int | None = None,
    target_id: int | None = None,
    scope: str | None = None,
    dry_run: bool = False,
    operator_approval_token: str | None = None,
    db=None,
    job_marker: str | None = None,
    job_id: int | None = None,
    skip_audit: bool = False,
) -> ExecutionGuardDecision:
    """
    Central execution guard. Default result is DENY unless explicit checks pass.
    """
    action = (action_type or "").strip().lower()
    if action not in ACTION_TYPES:
        decision = _deny(action or "unknown", "unknown_action", f"Unknown action: {action_type!r}")
        if not skip_audit:
            _log_guard_decision(decision, account_id=account_id, target_id=target_id)
        return decision

    audit_base: dict[str, Any] = {
        "dry_run": bool(dry_run),
        "scope": scope,
        "account_id": account_id,
        "target_id": target_id,
        "job_marker": job_marker,
    }

    if execution_emergency_lock_active():
        decision = _deny(
            action,
            "execution_emergency_lock",
            "EXECUTION_EMERGENCY_LOCK is active; all live execution blocked.",
            audit=audit_base,
        )
        if not skip_audit:
            _log_guard_decision(decision)
        return decision

    if dry_run:
        decision = _allow(
            action,
            "dry_run",
            "Dry-run only; no live side effects.",
            audit={**audit_base, "telethon_call": False},
        )
        if not skip_audit:
            _log_guard_decision(decision)
        return decision

    if _operator_approval_required() and not _operator_approval_ok(operator_approval_token):
        decision = _deny(
            action,
            "operator_approval_required",
            "Operator approval token required for live execution.",
            audit=audit_base,
        )
        if not skip_audit:
            _log_guard_decision(decision)
        return decision

    blocked, blockers = _account_blocked(account_id)
    if blocked:
        decision = _deny(
            action,
            "account_governance_block",
            "Account is protected, held, or AI-reserved.",
            blockers=blockers,
            audit=audit_base,
        )
        if not skip_audit:
            _log_guard_decision(decision)
        return decision

    if action == ACTION_TELEGRAM_SEND:
        from src.core.scheduler_models import (
            SCHEDULED_JOB_P4C_CERTIFICATION_MARKER,
            SCHEDULED_JOB_P5A_CERTIFICATION_MARKER,
            SCHEDULED_JOB_P5C_CERTIFICATION_MARKER,
            SCHEDULED_JOB_P5D_CERTIFICATION_MARKER,
        )
        from src.dashboard.scheduler_mutations import (
            is_scoped_campaign_pilot_run_now_allowed,
            is_scoped_send_test_run_now_allowed,
            scheduler_mutations_enabled,
        )

        scoped_ok = False
        if account_id is not None:
            if is_scoped_send_test_run_now_allowed(int(account_id)):
                scoped_ok = True
            elif target_id is not None and is_scoped_campaign_pilot_run_now_allowed(
                int(account_id), int(target_id)
            ):
                scoped_ok = True

        if (
            scoped_ok
            and str(job_marker or "").strip() == SCHEDULED_JOB_P4C_CERTIFICATION_MARKER
        ):
            p4c_enabled = os.environ.get("P4C_SINGLE_SEND_ENABLED", "false").strip().lower() in (
                "1",
                "true",
                "yes",
                "on",
            )
            if not p4c_enabled:
                decision = _deny(
                    action,
                    "p4c_single_send_disabled",
                    "P4C certification send blocked (P4C_SINGLE_SEND_ENABLED=false).",
                    audit=audit_base,
                )
                if not skip_audit:
                    _log_guard_decision(decision)
                return decision
            try:
                from src.core.p4c_send_counter import p4c_live_send_remaining

                remaining = p4c_live_send_remaining()
            except Exception as exc:
                decision = _deny(
                    action,
                    "p4c_counter_error",
                    f"P4C send counter check failed: {exc}",
                    audit=audit_base,
                )
                if not skip_audit:
                    _log_guard_decision(decision)
                return decision
            if remaining <= 0:
                decision = _deny(
                    action,
                    "p4c_send_limit_reached",
                    "P4C single-send limit reached; no further live sends allowed.",
                    audit={**audit_base, "p4c_remaining": remaining},
                )
                if not skip_audit:
                    _log_guard_decision(decision)
                return decision
            audit_base["p4c_remaining_before"] = remaining

        if (
            scoped_ok
            and str(job_marker or "").strip() == SCHEDULED_JOB_P5A_CERTIFICATION_MARKER
        ):
            from src.core.p5a_authorization import validate_p5a_authorization, p5a_single_send_enabled

            if not p5a_single_send_enabled():
                decision = _deny(
                    action,
                    "p5a_scope_inactive",
                    "P5A scope inactive (P5A_SINGLE_SEND_ENABLED=false).",
                    audit=audit_base,
                )
                if not skip_audit:
                    _log_guard_decision(decision)
                return decision
            ok, reason, p5a_audit = validate_p5a_authorization(
                account_id=account_id,
                target_id=target_id,
                job_marker=job_marker,
                job_id=job_id,
                require_armed=True,
                allow_consumed=False,
            )
            audit_base.update(p5a_audit)
            if not ok:
                decision = _deny(
                    action,
                    reason,
                    f"P5A authorization blocked: {reason}",
                    audit=audit_base,
                )
                if not skip_audit:
                    _log_guard_decision(decision)
                return decision
            audit_base["p5a_authorized"] = True

        if (
            scoped_ok
            and str(job_marker or "").strip() == SCHEDULED_JOB_P5C_CERTIFICATION_MARKER
        ):
            from src.core.p5c_authorization import validate_p5c_authorization, p5c_single_send_enabled

            if not p5c_single_send_enabled():
                decision = _deny(
                    action,
                    "p5c_scope_inactive",
                    "P5C scope inactive (P5C_SINGLE_SEND_ENABLED=false).",
                    audit=audit_base,
                )
                if not skip_audit:
                    _log_guard_decision(decision)
                return decision
            ok, reason, p5c_audit = validate_p5c_authorization(
                account_id=account_id,
                target_id=target_id,
                job_marker=job_marker,
                job_id=job_id,
                require_armed=True,
                allow_consumed=False,
            )
            audit_base.update(p5c_audit)
            if not ok:
                decision = _deny(
                    action,
                    reason,
                    f"P5C authorization blocked: {reason}",
                    audit=audit_base,
                )
                if not skip_audit:
                    _log_guard_decision(decision)
                return decision
            audit_base["p5c_authorized"] = True

        if (
            scoped_ok
            and str(job_marker or "").strip() == SCHEDULED_JOB_P5D_CERTIFICATION_MARKER
        ):
            from src.core.p5d_authorization import validate_p5d_authorization, p5d_single_send_enabled

            if not p5d_single_send_enabled():
                decision = _deny(
                    action,
                    "p5d_scope_inactive",
                    "P5D scope inactive (P5D_SINGLE_SEND_ENABLED=false).",
                    audit=audit_base,
                )
                if not skip_audit:
                    _log_guard_decision(decision)
                return decision
            ok, reason, p5d_audit = validate_p5d_authorization(
                account_id=account_id,
                target_id=target_id,
                job_marker=job_marker,
                job_id=job_id,
                require_armed=True,
                allow_consumed=False,
            )
            audit_base.update(p5d_audit)
            if not ok:
                decision = _deny(
                    action,
                    reason,
                    f"P5D authorization blocked: {reason}",
                    audit=audit_base,
                )
                if not skip_audit:
                    _log_guard_decision(decision)
                return decision
            audit_base["p5d_authorized"] = True

        if not scheduler_mutations_enabled() and not scoped_ok:
            decision = _deny(
                action,
                "scheduler_mutations_disabled",
                "Scheduler mutations disabled; telegram send blocked.",
                audit=audit_base,
            )
        elif job_marker and str(job_marker).strip() == "campaign_pilot_5":
            from src.scheduler.campaign_governance import check_governed_campaign_send_allowed

            if db is None or account_id is None or target_id is None:
                decision = _deny(
                    action,
                    "campaign_context_missing",
                    "Campaign send requires db, account_id, and target_id.",
                    audit=audit_base,
                )
            else:
                ok, err = check_governed_campaign_send_allowed(
                    db, int(account_id), int(target_id), job_marker=job_marker
                )
                if not ok:
                    decision = _deny(
                        action,
                        err or "campaign_not_allowed",
                        f"Campaign governance blocked send: {err}",
                        audit=audit_base,
                    )
                else:
                    decision = _allow(action, "campaign_send_ok", "Governed campaign send permitted.", audit=audit_base)
        elif audit_base.get("p5a_authorized"):
            decision = _allow(
                action,
                "p5a_scoped_repeatability_authorized",
                "P5A scoped repeatability send permitted.",
                audit=audit_base,
            )
        elif audit_base.get("p5c_authorized"):
            decision = _allow(
                action,
                "p5c_scoped_second_target_authorized",
                "P5C scoped second-target send permitted.",
                audit=audit_base,
            )
        elif audit_base.get("p5d_authorized"):
            decision = _allow(
                action,
                "p5d_gateway_restart_authorized",
                "P5D gateway restart durability send permitted.",
                audit=audit_base,
            )
        elif (
            str(job_marker or "").strip() == SCHEDULED_JOB_P5D_CERTIFICATION_MARKER
            and not audit_base.get("p5d_authorized")
        ):
            decision = _deny(
                action,
                "p5d_scope_inactive",
                "P5D certification send requires active scoped authorization.",
                audit=audit_base,
            )
        elif (
            str(job_marker or "").strip() == SCHEDULED_JOB_P5C_CERTIFICATION_MARKER
            and not audit_base.get("p5c_authorized")
        ):
            decision = _deny(
                action,
                "p5c_scope_inactive",
                "P5C certification send requires active scoped authorization.",
                audit=audit_base,
            )
        elif (
            str(job_marker or "").strip() == SCHEDULED_JOB_P5A_CERTIFICATION_MARKER
            and not audit_base.get("p5a_authorized")
        ):
            decision = _deny(
                action,
                "p5a_scope_inactive",
                "P5A certification send requires active scoped authorization.",
                audit=audit_base,
            )
        else:
            readiness = _account_readiness_blockers(db, account_id)
            if readiness and not scoped_ok:
                decision = _deny(
                    action,
                    "account_not_ready",
                    "Account readiness blocks send.",
                    blockers=readiness,
                    audit=audit_base,
                )
            else:
                decision = _allow(action, "telegram_send_ok", "Send permitted.", audit=audit_base)

    elif action == ACTION_TELEGRAM_JOIN:
        from src.dashboard.scheduler_mutations import scheduler_mutations_enabled

        if not scheduler_mutations_enabled():
            decision = _deny(
                action,
                "scheduler_mutations_disabled",
                "Join blocked while scheduler mutations disabled.",
                audit=audit_base,
            )
        else:
            decision = _allow(action, "telegram_join_ok", "Join permitted.", audit=audit_base)

    elif action == ACTION_STORY_PUBLISH:
        from src.stories.scheduler_integration import story_execution_enabled

        if not story_execution_enabled():
            decision = _deny(
                action,
                "story_execution_disabled",
                "STORY_EXECUTION_ENABLED=false; story publish blocked.",
                audit=audit_base,
            )
        else:
            decision = _allow(action, "story_publish_ok", "Story publish permitted.", audit=audit_base)

    elif action == ACTION_CAMPAIGN_EXECUTE:
        from src.scheduler.campaign_governance import campaign_execution_enabled

        if not campaign_execution_enabled():
            decision = _deny(
                action,
                "campaign_execution_disabled",
                "CAMPAIGN_EXECUTION_ENABLED=false; campaign execute blocked.",
                audit=audit_base,
            )
        elif db is not None and account_id is not None and target_id is not None:
            from src.scheduler.campaign_governance import check_governed_campaign_send_allowed

            ok, err = check_governed_campaign_send_allowed(
                db, int(account_id), int(target_id), job_marker=job_marker
            )
            if not ok:
                decision = _deny(
                    action,
                    err or "campaign_not_allowed",
                    f"Campaign governance blocked: {err}",
                    audit=audit_base,
                )
            else:
                decision = _allow(action, "campaign_execute_ok", "Campaign execute permitted.", audit=audit_base)
        else:
            decision = _deny(
                action,
                "campaign_context_missing",
                "Campaign execute requires db, account_id, and target_id.",
                audit=audit_base,
            )

    elif action in (ACTION_DISCOVERY_SCAN, ACTION_DISCOVERY_JOIN):
        if not discovery_execution_enabled():
            decision = _deny(
                action,
                "discovery_execution_disabled",
                "DISCOVERY_EXECUTION_ENABLED=false; live discovery blocked.",
                audit=audit_base,
            )
        else:
            readiness = _account_readiness_blockers(db, account_id)
            if readiness:
                decision = _deny(
                    action,
                    "discovery_account_not_ready",
                    "Discovery account not ready.",
                    blockers=readiness,
                    audit=audit_base,
                )
            else:
                decision = _allow(
                    action,
                    "discovery_ok",
                    "Discovery live action permitted.",
                    audit=audit_base,
                )

    elif action == ACTION_ACCOUNT_MUTATION:
        from src.dashboard.scheduler_mutations import scheduler_mutations_enabled

        if not scheduler_mutations_enabled():
            decision = _deny(
                action,
                "scheduler_mutations_disabled",
                "Account mutation blocked while scheduler mutations disabled.",
                audit=audit_base,
            )
        else:
            decision = _allow(action, "account_mutation_ok", "Account mutation permitted.", audit=audit_base)

    elif action == ACTION_AI_CODING_EXECUTE:
        raw = (os.environ.get("AI_CODING_EXECUTE_ENABLED") or "false").strip().lower()
        if raw not in ("1", "true", "yes", "on"):
            decision = _deny(
                action,
                "ai_coding_execute_disabled",
                "AI coding execute disabled by default.",
                audit=audit_base,
            )
        else:
            decision = _allow(action, "ai_coding_execute_ok", "AI coding execute permitted.", audit=audit_base)

    else:
        decision = _deny(action, "unhandled_action", "No handler for action.", audit=audit_base)

    if not skip_audit:
        _log_guard_decision(decision)
    return decision


def build_execution_lock_matrix() -> dict[str, Any]:
    """Read-only matrix for /api/health/deep and soak gates."""
    from src.dashboard.scheduler_mutations import (
        scheduler_mutations_enabled,
        scoped_campaign_pilot_allowlist_active,
        scoped_send_test_allowlist_active,
    )
    from src.scheduler.campaign_governance import campaign_execution_enabled
    from src.stories.scheduler_integration import story_execution_enabled

    return {
        "execution_emergency_lock": execution_emergency_lock_active(),
        "scheduler_mutations_enabled": scheduler_mutations_enabled(),
        "story_execution_enabled": story_execution_enabled(),
        "campaign_execution_enabled": campaign_execution_enabled(),
        "discovery_execution_enabled": discovery_execution_enabled(),
        "ai_coding_execute_enabled": os.environ.get("AI_CODING_EXECUTE_ENABLED", "false")
        .strip()
        .lower()
        in ("1", "true", "yes", "on"),
        "scoped_send_test_active": scoped_send_test_allowlist_active(),
        "scoped_campaign_pilot_active": scoped_campaign_pilot_allowlist_active(),
        "default_policy": RESULT_DENY,
        "action_types": sorted(ACTION_TYPES),
    }


def require_execution_allowed(
    action_type: str,
    *,
    account_id: int | None = None,
    target_id: int | None = None,
    scope: str | None = None,
    dry_run: bool = False,
    db=None,
    job_marker: str | None = None,
    job_id: int | None = None,
) -> ExecutionGuardDecision | None:
    """
    Return None when action may proceed; otherwise the DENY decision (audit logged).
    """
    decision = can_execute_action(
        action_type,
        account_id=account_id,
        target_id=target_id,
        scope=scope,
        dry_run=dry_run,
        db=db,
        job_marker=job_marker,
        job_id=job_id,
    )
    return None if decision.allowed else decision


def guard_blocked_ai_send(decision: ExecutionGuardDecision) -> dict[str, Any]:
    return {
        "ok": False,
        "telegram_message_id": None,
        "error_code": decision.reason_code,
        "error_message": decision.message,
        "execution_guard": decision.to_dict(),
    }


def guard_blocked_story_publish(decision: ExecutionGuardDecision) -> dict[str, Any]:
    return {
        "success": False,
        "error": decision.message,
        "error_code": decision.reason_code,
        "execution_guard": decision.to_dict(),
    }


def guard_blocked_join(decision: ExecutionGuardDecision, *, base: dict | None = None) -> dict[str, Any]:
    out = dict(base or {})
    out.update(
        {
            "status": "failed",
            "error": decision.message,
            "message": decision.message,
            "error_code": decision.reason_code,
            "execution_guard": decision.to_dict(),
        }
    )
    return out


def guard_blocked_discovery(decision: ExecutionGuardDecision) -> dict[str, Any]:
    return {
        "success": False,
        "error": decision.message,
        "error_code": decision.reason_code,
        "execution_guard": decision.to_dict(),
    }


def flask_guard_response(decision: ExecutionGuardDecision, *, status: int = 403) -> tuple[Any, int]:
    from flask import jsonify

    return (
        jsonify(
            {
                "ok": False,
                "error": decision.reason_code,
                "message": decision.message,
                "execution_guard": decision.to_dict(),
            }
        ),
        status,
    )
