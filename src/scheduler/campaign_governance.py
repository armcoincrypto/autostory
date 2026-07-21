"""
P9.71 — Campaign governance: state machine, cohort registry, audit, execution gates.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.orm import Session

from config.settings import settings
from src.core.campaign_governance_models import CampaignGovernance
from src.core.models import Account
from src.core.scheduler_models import (
    AccountTargetBinding,
    ChatTarget,
    JobStatus,
    ScheduledJob,
    SCHEDULED_JOB_CAMPAIGN_PILOT_MARKER,
)
from src.recovery.pilot_ready_accounts import PROHIBITED_PILOT_IDS
from src.recovery.recovery_lab import is_prohibited_account
from src.scheduler.campaign_pilot_template import (
    approved_pilot_body,
    approved_pilot_body_sha256,
    pilot_template_name,
)

PHASE = "P9.71"

# P9.73 — states that may persist an emergency abort (not draft/in_review).
PERSIST_ABORT_STATES = frozenset({"approved", "armed", "executing"})

# P9.81 — governed run-now / send gate accepts campaign in armed or executing.
GOVERNED_ACTIVE_SEND_STATES = frozenset({"armed", "executing"})

SCOPE_TIER_LIMITS: dict[str, int] = {
    "tiny_5": 5,
    "small_10": 10,
    "medium_20": 20,
}

VALID_SCOPE_TIERS = frozenset(SCOPE_TIER_LIMITS)
VALID_STATES = frozenset(
    {
        "draft",
        "in_review",
        "approved",
        "armed",
        "executing",
        "completed",
        "aborted",
    }
)

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"in_review"}),
    "in_review": frozenset({"approved"}),
    "approved": frozenset({"armed", "aborted"}),
    "armed": frozenset({"executing", "aborted"}),
    "executing": frozenset({"completed", "aborted"}),
    "completed": frozenset(),
    "aborted": frozenset(),
}

PURPOSE_HOLD_IDS = frozenset({34, 36, 101})
PROTECTED_IDS = frozenset(PROHIBITED_PILOT_IDS)
EXCLUDED_ACCOUNT_IDS = PURPOSE_HOLD_IDS | PROTECTED_IDS

DEFAULT_COHORT_REGISTRY_PATH = Path("data/recovery_lab/campaign_cohort_registry.json")
AUDIT_LOG_PATH = Path("data/audit/campaign_governance.jsonl")


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def campaign_execution_enabled() -> bool:
    return bool(getattr(settings, "campaign_execution_enabled", False))


def _normalize_pair_map(raw: dict[Any, Any] | list[Any]) -> dict[int, int]:
    if isinstance(raw, list):
        out: dict[int, int] = {}
        for item in raw:
            if isinstance(item, dict):
                out[int(item["account_id"])] = int(item["target_id"])
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                out[int(item[0])] = int(item[1])
        return out
    out = {}
    for k, v in (raw or {}).items():
        out[int(k)] = int(v)
    return out


def _pair_map_to_json(pairs: dict[int, int]) -> dict[str, int]:
    return {str(k): int(v) for k, v in sorted(pairs.items())}


def cohort_template_manifest_hash(pairs: dict[int, int]) -> str:
    """SHA-256 of per-account approved template hashes (deterministic cohort fingerprint)."""
    parts = {str(a): approved_pilot_body_sha256(int(a)) for a in sorted(pairs)}
    payload = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def account_template_hash_matches_cohort(campaign: CampaignGovernance, account_id: int) -> bool:
    pairs = _normalize_pair_map(campaign.allowed_pair_map or {})
    aid = int(account_id)
    if aid not in pairs:
        return False
    return (campaign.approved_template_hash or "") == cohort_template_manifest_hash(pairs)


def load_cohort_registry(path: Path | None = None) -> dict[str, Any]:
    p = path or DEFAULT_COHORT_REGISTRY_PATH
    if not p.is_file():
        return {"cohorts": [], "used_account_ids": []}
    return json.loads(p.read_text(encoding="utf-8"))


def registry_used_account_ids(registry: dict[str, Any]) -> frozenset[int]:
    used: set[int] = set()
    for c in registry.get("cohorts") or []:
        for aid in c.get("account_ids") or []:
            used.add(int(aid))
    for aid in registry.get("used_account_ids") or []:
        used.add(int(aid))
    return frozenset(used)


def append_audit_event(
    event: str,
    *,
    campaign_id: int | None = None,
    extra: dict[str, Any] | None = None,
    path: Path | None = None,
    audit_phase: str | None = None,
) -> None:
    log_path = path or AUDIT_LOG_PATH
    log_path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "phase": audit_phase or PHASE,
        "event": event,
        "campaign_id": campaign_id,
        **(extra or {}),
    }
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")


def check_arm_transition_allowed(
    *,
    dry_run: bool = False,
    simulation: bool = False,
) -> tuple[bool, str | None]:
    """Real arm requires CAMPAIGN_EXECUTION_ENABLED; simulation/dry-run may proceed."""
    if dry_run or simulation:
        return True, None
    if not campaign_execution_enabled():
        return False, "arm_blocked_execution_disabled"
    return True, None


def check_campaign_execute_allowed(
    db: Session,
    campaign: CampaignGovernance,
    *,
    allow_cohort_reuse: bool = False,
    classify_fn: Any | None = None,
    registry_path: Path | None = None,
) -> tuple[bool, str | None, dict[str, Any]]:
    """
    Fail-closed execute gate: armed/executing + execution enabled + template manifest + GREEN.
    """
    audit: dict[str, Any] = {
        "campaign_id": int(campaign.id) if campaign.id else None,
        "state": campaign.state,
        "campaign_execution_enabled": campaign_execution_enabled(),
    }
    state = (campaign.state or "").strip().lower()
    if state not in GOVERNED_ACTIVE_SEND_STATES:
        return False, f"execute_blocked_state_not_active:{state}", audit

    if not campaign_execution_enabled():
        return False, "campaign_execution_disabled", audit

    pairs = _normalize_pair_map(campaign.allowed_pair_map or {})
    registry = load_cohort_registry(registry_path)
    used = registry_used_account_ids(registry)
    overlap = sorted(set(pairs) & used)
    audit["cohort_overlap"] = overlap
    audit["allow_cohort_reuse"] = allow_cohort_reuse
    if overlap and not allow_cohort_reuse:
        return False, f"cohort_reuse_blocked:{overlap}", audit

    manifest = cohort_template_manifest_hash(pairs)
    stored = (campaign.approved_template_hash or "").strip()
    audit["approved_template_hash"] = stored
    audit["current_manifest_hash"] = manifest
    if stored != manifest:
        return False, "campaign_template_manifest_changed_after_approval", audit

    non_green: list[int] = []
    for aid in sorted(pairs):
        if classify_fn:
            row = classify_fn(db, aid)
            cls = row.get("classification_post_rebaseline") or row.get("classification")
            if cls != "GREEN":
                non_green.append(int(aid))
        elif aid in EXCLUDED_ACCOUNT_IDS:
            non_green.append(int(aid))
    audit["non_green_account_ids"] = non_green
    if non_green:
        return False, f"execute_blocked_accounts_not_green:{non_green}", audit

    return True, None, audit


def simulate_transition_path(
    initial_state: str,
    steps: list[str],
    *,
    execution_enabled: bool = False,
    simulation: bool = True,
) -> dict[str, Any]:
    """
    Pure state-machine walk (no DB). Used to prove valid/invalid transition chains.
    """
    state = (initial_state or "draft").strip().lower()
    results: list[dict[str, Any]] = []
    for step in steps:
        nxt = (step or "").strip().lower()
        ok = can_transition(state, nxt)
        block_reason: str | None = None
        if ok and nxt == "armed" and not simulation and not execution_enabled:
            ok = False
            block_reason = "arm_blocked_execution_disabled"
        if ok and nxt == "executing" and not execution_enabled:
            ok = False
            block_reason = "execute_blocked_execution_disabled"
        if ok and nxt == "armed" and execution_enabled and not simulation:
            pass
        results.append(
            {
                "from": state,
                "to": nxt,
                "allowed": ok,
                "block_reason": block_reason,
            }
        )
        if ok:
            state = nxt
    return {
        "initial_state": initial_state,
        "final_state": state,
        "steps": results,
        "simulation": simulation,
        "execution_enabled": execution_enabled,
    }


def simulate_transition_chain(
    db: Session,
    campaign: CampaignGovernance,
    steps: list[str],
    *,
    actor: str | None = None,
    dry_run: bool = True,
    simulation: bool = True,
) -> dict[str, Any]:
    """Apply transition_campaign in dry-run/simulation mode for each step."""
    start = (campaign.state or "").strip().lower()
    results: list[dict[str, Any]] = []
    for step in steps:
        nxt = (step or "").strip().lower()
        prev = (campaign.state or "").strip().lower()
        ok, err = transition_campaign(
            db,
            campaign,
            nxt,
            actor=actor,
            reason=None,
            dry_run=dry_run,
            simulation=simulation,
        )
        results.append({"from": prev, "to": nxt, "ok": ok, "error": err})
        if not ok:
            break
    return {
        "campaign_id": int(campaign.id) if campaign.id else None,
        "initial_state": start,
        "final_state": (campaign.state or "").strip().lower(),
        "steps": results,
        "dry_run": dry_run,
        "simulation": simulation,
    }


def can_transition(current: str, new: str) -> bool:
    cur = (current or "").strip().lower()
    nxt = (new or "").strip().lower()
    if cur not in VALID_STATES or nxt not in VALID_STATES:
        return False
    return nxt in ALLOWED_TRANSITIONS.get(cur, frozenset())


def transition_campaign(
    db: Session,
    campaign: CampaignGovernance,
    new_state: str,
    *,
    actor: str | None = None,
    reason: str | None = None,
    dry_run: bool = False,
    simulation: bool = False,
) -> tuple[bool, str | None]:
    """Apply state transition; return (ok, error_code)."""
    cur = (campaign.state or "").strip().lower()
    nxt = (new_state or "").strip().lower()
    if not can_transition(cur, nxt):
        append_audit_event(
            "invalid_transition",
            campaign_id=int(campaign.id) if campaign.id else None,
            extra={"from": cur, "to": nxt, "actor": actor, "reason": reason, "simulation": simulation},
        )
        return False, f"invalid_transition:{cur}_to_{nxt}"

    if nxt == "armed":
        ok, err = check_arm_transition_allowed(dry_run=dry_run, simulation=simulation)
        if not ok:
            append_audit_event(
                "arm_blocked",
                campaign_id=int(campaign.id) if campaign.id else None,
                extra={"from": cur, "to": nxt, "reason": err, "simulation": simulation},
            )
            return False, err

    if nxt == "executing" and not dry_run and not simulation:
        ok, err, _ = check_campaign_execute_allowed(db, campaign)
        if not ok:
            append_audit_event(
                "execute_blocked",
                campaign_id=int(campaign.id) if campaign.id else None,
                extra={"from": cur, "to": nxt, "reason": err},
            )
            return False, err

    now = _utc_now_naive()
    campaign.state = nxt
    if nxt == "approved":
        campaign.approved_at = now
        if actor:
            campaign.approved_by = actor
    elif nxt == "armed":
        campaign.armed_at = now
    elif nxt == "completed":
        campaign.completed_at = now
    elif nxt == "aborted":
        campaign.aborted_at = now
        if reason:
            campaign.emergency_stop_reason = reason

    if not dry_run:
        db.add(campaign)
        db.commit()
        db.refresh(campaign)
        append_audit_event(
            "state_transition",
            campaign_id=int(campaign.id),
            extra={
                "from": cur,
                "to": nxt,
                "actor": actor,
                "reason": reason,
                "simulation": simulation,
                "dry_run": dry_run,
            },
        )
    return True, None


def create_campaign_governance(
    db: Session,
    *,
    name: str,
    scope_tier: str,
    pair_map: dict[int, int],
    template_body: str | None = None,
    created_by: str | None = None,
    safety_notes: str | None = None,
    dry_run: bool = False,
) -> CampaignGovernance:
    tier = scope_tier.strip().lower()
    if tier not in VALID_SCOPE_TIERS:
        raise ValueError(f"invalid_scope_tier:{tier}")
    pairs = _normalize_pair_map(pair_map)
    limit = SCOPE_TIER_LIMITS[tier]
    if len(pairs) != limit:
        raise ValueError(f"pair_count_mismatch:expected_{limit}_got_{len(pairs)}")
    if len(pairs) != len(set(pairs.keys())):
        raise ValueError("duplicate_accounts_in_pair_map")

    if template_body and template_body.strip():
        for aid in pairs:
            if approved_pilot_body(aid) != template_body.strip():
                raise ValueError(f"template_body_mismatch_account_{aid}")

    accounts = sorted(pairs.keys())
    targets = sorted(pairs.values())
    for aid in accounts:
        if aid in EXCLUDED_ACCOUNT_IDS:
            raise ValueError(f"excluded_account:{aid}")

    row = CampaignGovernance(
        name=name.strip(),
        scope_tier=tier,
        state="draft",
        allowed_account_ids=accounts,
        allowed_target_ids=targets,
        allowed_pair_map=_pair_map_to_json(pairs),
        approved_template_body=None,
        approved_template_hash=cohort_template_manifest_hash(pairs),
        created_by=created_by,
        safety_notes=safety_notes,
        created_at=_utc_now_naive(),
    )
    if not dry_run:
        db.add(row)
        db.commit()
        db.refresh(row)
        append_audit_event(
            "campaign_created",
            campaign_id=int(row.id),
            extra={
                "name": row.name,
                "scope_tier": tier,
                "account_ids": accounts,
                "pair_map": _pair_map_to_json(pairs),
            },
        )
    return row


def find_armed_campaign_for_pair(
    db: Session,
    account_id: int,
    target_id: int,
) -> CampaignGovernance | None:
    """
    Find governed campaign for exact pair in armed or executing state (P9.81).

    Name retained for API compatibility; matches GOVERNED_ACTIVE_SEND_STATES only.
    """
    aid, tid = int(account_id), int(target_id)
    rows = (
        db.query(CampaignGovernance)
        .filter(CampaignGovernance.state.in_(sorted(GOVERNED_ACTIVE_SEND_STATES)))
        .order_by(CampaignGovernance.id.desc())
        .all()
    )
    for row in rows:
        pairs = _normalize_pair_map(row.allowed_pair_map or {})
        if pairs.get(aid) == tid:
            return row
    return None


def check_governed_campaign_send_allowed(
    db: Session,
    account_id: int,
    target_id: int,
    *,
    job_marker: str | None = None,
) -> tuple[bool, str | None]:
    """
    Fail-closed gate for governed campaign sends (run-now / executor).
    """
    if (job_marker or "").strip() != SCHEDULED_JOB_CAMPAIGN_PILOT_MARKER:
        from src.dashboard.scheduler_mutations import is_scoped_campaign_pilot_run_now_allowed

        if not is_scoped_campaign_pilot_run_now_allowed(int(account_id), int(target_id)):
            return False, "not_scoped_campaign_pilot"

    if not campaign_execution_enabled():
        return False, "campaign_execution_disabled"

    campaign = find_armed_campaign_for_pair(db, int(account_id), int(target_id))
    if not campaign:
        return False, "no_governed_campaign_for_pair"

    pairs = _normalize_pair_map(campaign.allowed_pair_map or {})
    aid = int(account_id)
    if pairs.get(aid) != int(target_id):
        return False, "campaign_pair_mismatch"

    ok, err, _audit = check_campaign_execute_allowed(db, campaign)
    if not ok:
        return False, err

    if not account_template_hash_matches_cohort(campaign, aid):
        return False, "campaign_account_template_hash_mismatch"

    return True, None


def reset_campaign_for_retry(
    db: Session,
    campaign_id: int,
    *,
    reason: str,
    actor: str | None = None,
    allowed_from: frozenset[str] | None = None,
) -> tuple[bool, str | None, dict[str, Any]]:
    """
    Operator reset after failed execute with no sends (P9.81).

    Sets state to draft without erasing emergency_stop_reason or other history.
    Not a normal state-machine transition (aborted has no outbound edges).
    """
    allowed = allowed_from or frozenset({"aborted"})
    row = db.query(CampaignGovernance).filter(CampaignGovernance.id == int(campaign_id)).first()
    if not row:
        return False, "campaign_not_found", {}
    cur = (row.state or "").strip().lower()
    if cur not in allowed:
        return False, f"reset_blocked_from_{cur}", {"prior_state": cur}

    prior = {
        "prior_state": cur,
        "prior_emergency_stop_reason": row.emergency_stop_reason,
        "prior_approved_template_hash": row.approved_template_hash,
    }
    row.state = "draft"
    db.add(row)
    db.commit()
    db.refresh(row)
    append_audit_event(
        "retry_reset",
        campaign_id=int(campaign_id),
        extra={
            "reason": reason,
            "actor": actor,
            **prior,
            "new_state": "draft",
        },
        audit_phase="P9.81",
    )
    return True, None, prior


def request_emergency_stop(
    db: Session,
    campaign_id: int,
    *,
    reason: str,
    actor: str | None = None,
    dry_run: bool = False,
) -> tuple[bool, str | None]:
    row = db.query(CampaignGovernance).filter(CampaignGovernance.id == int(campaign_id)).first()
    if not row:
        return False, "campaign_not_found"
    cur = (row.state or "").strip().lower()
    if cur in ("completed", "aborted"):
        return False, f"cannot_abort_from_{cur}"

    if not dry_run and cur not in PERSIST_ABORT_STATES:
        append_audit_event(
            "emergency_stop_rejected",
            campaign_id=int(campaign_id),
            extra={"reason": reason, "actor": actor, "prior_state": cur},
        )
        return False, f"cannot_persist_abort_from_{cur}"

    append_audit_event(
        "emergency_stop_requested",
        campaign_id=int(campaign_id),
        extra={"reason": reason, "actor": actor, "prior_state": cur, "dry_run": dry_run},
    )
    return transition_campaign(
        db, row, "aborted", actor=actor, reason=reason, dry_run=dry_run, simulation=dry_run
    )


def _is_operator_self_target(db: Session, target: ChatTarget, account: Account) -> tuple[bool, str]:
    if (target.chat_type or "").lower() != "private":
        return False, f"chat_type={target.chat_type}"
    uid = int(account.user_id or 0)
    if uid and target.tg_id and int(target.tg_id) == uid:
        return True, "tg_id_matches_user_id"
    title = (target.title or "").lower()
    if "saved messages" in title and uid:
        return True, "saved_messages_title"
    return False, f"private_tg_id={target.tg_id}_user_id={uid}"


def _pending_due_count(db: Session, account_id: int) -> int:
    now = _utc_now_naive()
    return int(
        db.query(ScheduledJob)
        .filter(
            ScheduledJob.account_id == int(account_id),
            ScheduledJob.status == JobStatus.PENDING.value,
            ScheduledJob.run_at <= now,
        )
        .count()
    )


def validate_proposed_cohort(
    db: Session,
    *,
    scope_tier: str,
    pair_map: dict[int, int],
    allow_cohort_reuse: bool = False,
    registry_path: Path | None = None,
    classify_fn: Any | None = None,
) -> dict[str, Any]:
    """Read-only validation for preflight GO/NO-GO."""
    blockers: list[str] = []
    tier = scope_tier.strip().lower()
    pairs = _normalize_pair_map(pair_map)

    if tier not in VALID_SCOPE_TIERS:
        blockers.append(f"invalid_scope_tier:{tier}")
    limit = SCOPE_TIER_LIMITS.get(tier, 0)
    if len(pairs) != limit:
        blockers.append(f"pair_count_not_{limit}")

    registry = load_cohort_registry(registry_path)
    used = registry_used_account_ids(registry)
    overlap = sorted(set(pairs) & used)
    if overlap and not allow_cohort_reuse:
        blockers.append(f"cohort_reuse_blocked:{overlap}")

    account_audits: list[dict[str, Any]] = []

    for aid, tid in sorted(pairs.items()):
        ablock: list[str] = []
        if aid in EXCLUDED_ACCOUNT_IDS:
            ablock.append("excluded_account")
        prohibited, reason = is_prohibited_account(aid)
        if prohibited:
            ablock.append(reason or "prohibited")

        acc = db.query(Account).filter(Account.id == int(aid)).first()
        target = db.query(ChatTarget).filter(ChatTarget.id == int(tid)).first()
        binding = (
            db.query(AccountTargetBinding)
            .filter(
                AccountTargetBinding.account_id == int(aid),
                AccountTargetBinding.target_id == int(tid),
            )
            .first()
        )
        if not acc:
            ablock.append("account_missing")
        if not target:
            ablock.append("target_missing")
        elif acc:
            ok, why = _is_operator_self_target(db, target, acc)
            if not ok:
                ablock.append(f"target_not_operator_self:{why}")
        if not binding:
            ablock.append("binding_missing")
        elif not binding.can_post:
            ablock.append("binding_can_post_false")

        if classify_fn and acc:
            row = classify_fn(db, aid)
            cls = row.get("classification_post_rebaseline") or row.get("classification")
            if cls != "GREEN":
                ablock.append(f"classification_{cls}")
        elif classify_fn:
            ablock.append("classification_unknown")

        pending = _pending_due_count(db, aid)
        if pending > 0:
            ablock.append(f"pending_due:{pending}")

        body = approved_pilot_body(aid)
        th = approved_pilot_body_sha256(aid)

        from src.core.scheduler_models import MessageType
        from src.scheduler.campaign_pilot_template import (
            resolve_campaign_pilot_template_body,
        )

        if acc and target and binding:
            job = ScheduledJob(
                account_id=aid,
                target_id=tid,
                type=MessageType.PROMO.value,
                last_error=SCHEDULED_JOB_CAMPAIGN_PILOT_MARKER,
            )
            _body, err = resolve_campaign_pilot_template_body(
                db, job, acc, target, binding, pairs=pairs
            )
            if err:
                ablock.append(err)

        account_audits.append(
            {
                "account_id": aid,
                "target_id": tid,
                "expected_template_hash": th,
                "template_name": pilot_template_name(aid),
                "body_preview": body[:80],
                "blockers": ablock,
                "ok": len(ablock) == 0,
            }
        )
        blockers.extend(ablock)

    manifest = cohort_template_manifest_hash(pairs) if pairs else ""
    preflight_go = len(blockers) == 0
    return {
        "approved_template_manifest_hash": manifest,
        "scope_tier": tier,
        "pair_map": _pair_map_to_json(pairs),
        "account_audits": account_audits,
        "blockers": sorted(set(blockers)),
        "preflight_go": preflight_go,
        "campaign_execution_enabled": campaign_execution_enabled(),
        "cohort_overlap": overlap,
        "allow_cohort_reuse": allow_cohort_reuse,
    }
