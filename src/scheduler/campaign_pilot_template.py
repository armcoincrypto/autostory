"""
P9.68 — Deterministic template selection for campaign_pilot_5 scoped sends.

When a job is a campaign pilot job, the executor must use exactly one approved
TARGET-scoped PROMO template whose body matches the canonical pilot message hash.
No random selection; fail closed on ambiguity or missing approval.
"""
from __future__ import annotations

import hashlib
from typing import Any, Optional

from sqlalchemy.orm import Session

from src.core.scheduler_models import (
    MessageTemplate,
    MessageType,
    SCHEDULED_JOB_CAMPAIGN_PILOT_MARKER,
    ScheduledJob,
)

PILOT_MESSAGE_PREFIX = "[P9.66 PILOT]"

# Legacy P9.66 / P9.67 pairs (fallback when env pair allowlist unset).
CAMPAIGN_PILOT_PAIRS: dict[int, int] = {
    111: 17,
    109: 16,
    107: 14,
    108: 15,
    112: 19,
}

P9_67_USED_ACCOUNT_IDS: frozenset[int] = frozenset({111, 109, 107, 108, 112})


def approved_pilot_body(account_id: int) -> str:
    aid = int(account_id)
    return (
        f"{PILOT_MESSAGE_PREFIX} Account {aid} tiny campaign pilot. "
        "Harmless operator-approved self-send proof. Single message only."
    )


def approved_pilot_body_sha256(account_id: int) -> str:
    body = approved_pilot_body(account_id)
    return hashlib.sha256(body.strip().encode("utf-8")).hexdigest()


def pilot_template_name(account_id: int) -> str:
    return f"P9.66 Tiny Campaign Pilot {account_id}"


def get_campaign_pilot_pairs() -> dict[int, int]:
    """Active pairs from env allowlist when scoped execute is configured."""
    try:
        from src.dashboard.scheduler_mutations import scheduler_mutation_pair_allowlist_map

        pairs = scheduler_mutation_pair_allowlist_map()
        if pairs:
            return dict(pairs)
    except Exception:
        pass
    return dict(CAMPAIGN_PILOT_PAIRS)


def is_campaign_pilot_job(job: ScheduledJob) -> bool:
    """True when job was queued under campaign_pilot_5 or scope requires deterministic body."""
    marker = str(job.last_error or "").strip()
    if marker == SCHEDULED_JOB_CAMPAIGN_PILOT_MARKER:
        return True
    try:
        from src.dashboard.scheduler_mutations import (
            is_scoped_campaign_pilot_run_now_allowed,
            scoped_campaign_pilot_allowlist_active,
        )

        if scoped_campaign_pilot_allowlist_active():
            return is_scoped_campaign_pilot_run_now_allowed(
                int(job.account_id), int(job.target_id)
            )
    except Exception:
        pass
    return False


def _body_hash(body: str) -> str:
    return hashlib.sha256((body or "").strip().encode("utf-8")).hexdigest()


def _active_promo_templates(
    db: Session,
    *,
    scope: str,
    scope_id: int | None,
) -> list[MessageTemplate]:
    q = db.query(MessageTemplate).filter(
        MessageTemplate.type == MessageType.PROMO.value,
        MessageTemplate.scope == scope,
        MessageTemplate.is_active == True,
    )
    if scope == "BINDING":
        q = q.filter(MessageTemplate.binding_id == int(scope_id))
    elif scope == "TARGET":
        q = q.filter(MessageTemplate.target_id == int(scope_id))
    elif scope == "ACCOUNT":
        q = q.filter(MessageTemplate.account_id == int(scope_id))
    elif scope == "GLOBAL":
        q = q.filter(
            MessageTemplate.account_id.is_(None),
            MessageTemplate.target_id.is_(None),
            MessageTemplate.binding_id.is_(None),
        )
    return q.order_by(MessageTemplate.weight.desc()).all()


def resolve_campaign_pilot_template_body(
    db: Session,
    job: ScheduledJob,
    account: Any,
    target: Any,
    binding: Any,
    *,
    pairs: dict[int, int] | None = None,
) -> tuple[Optional[str], Optional[str]]:
    """
    Return (body, None) on success or (None, error_code) fail-closed.
    """
    aid = int(account.id)
    tid = int(target.id)
    active_pairs = dict(pairs or get_campaign_pilot_pairs())
    expected_tid = active_pairs.get(aid)
    if expected_tid is None:
        return None, "campaign_pilot_account_not_in_approved_pairs"
    if tid != expected_tid:
        return None, "campaign_pilot_target_pair_mismatch"

    expected_body = approved_pilot_body(aid)
    expected_hash = approved_pilot_body_sha256(aid)
    expected_name = pilot_template_name(aid)

    # BINDING scope is evaluated before TARGET in legacy path and would override pilot body.
    binding_extras = _active_promo_templates(db, scope="BINDING", scope_id=int(binding.id))
    if binding_extras:
        return None, "campaign_pilot_extraneous_active_template:binding"

    target_templates = _active_promo_templates(db, scope="TARGET", scope_id=tid)
    if not target_templates:
        return None, "campaign_pilot_no_active_target_template"

    approved = [
        t
        for t in target_templates
        if _body_hash(t.body) == expected_hash
        and (t.name or "") == expected_name
    ]
    non_approved_active = [
        t
        for t in target_templates
        if _body_hash(t.body) != expected_hash
    ]

    if non_approved_active:
        return None, "campaign_pilot_non_approved_active_on_target"

    if len(approved) == 0:
        return None, "campaign_pilot_no_approved_template"

    if len(approved) > 1:
        return None, "campaign_pilot_ambiguous_approved_template"

    if _body_hash(approved[0].body) != expected_hash:
        return None, "campaign_pilot_body_hash_mismatch"

    return approved[0].body, None


def validate_account_target_preflight(
    db: Session,
    account_id: int,
    target_id: int,
    *,
    pairs: dict[int, int] | None = None,
) -> dict[str, Any]:
    """Read-only preflight for one approved pair (no DB writes)."""
    aid = int(account_id)
    tid = int(target_id)
    blockers: list[str] = []
    active_pairs = dict(pairs or get_campaign_pilot_pairs())
    expected_tid = active_pairs.get(aid)
    if expected_tid is None:
        blockers.append("account_not_in_approved_pairs")
    elif tid != expected_tid:
        blockers.append("target_pair_mismatch")

    from src.core.models import Account
    from src.core.scheduler_models import ChatTarget, AccountTargetBinding

    account = db.query(Account).filter(Account.id == aid).first()
    target = db.query(ChatTarget).filter(ChatTarget.id == tid).first()
    binding = (
        db.query(AccountTargetBinding)
        .filter(
            AccountTargetBinding.account_id == aid,
            AccountTargetBinding.target_id == tid,
        )
        .first()
    )

    if not account:
        blockers.append("account_missing")
    if not target:
        blockers.append("target_missing")
    if not binding:
        blockers.append("binding_missing")
    elif not binding.can_post:
        blockers.append("binding_can_post_false")

    template_audit: dict[str, Any] = {}
    if account and target and binding:
        job = ScheduledJob(
            account_id=aid,
            target_id=tid,
            type=MessageType.PROMO.value,
            last_error=SCHEDULED_JOB_CAMPAIGN_PILOT_MARKER,
        )
        body, err = resolve_campaign_pilot_template_body(
            db, job, account, target, binding, pairs=active_pairs
        )
        template_audit = {
            "resolve_error": err,
            "resolved_body_preview": (body or "")[:80] if body else None,
            "expected_hash": approved_pilot_body_sha256(aid),
        }
        if err:
            blockers.append(err)

    target_rows = _active_promo_templates(db, scope="TARGET", scope_id=tid) if target else []
    template_audit["target_active_templates"] = [
        {
            "id": int(t.id),
            "name": t.name,
            "weight": t.weight,
            "body_hash": _body_hash(t.body),
            "is_approved": _body_hash(t.body) == approved_pilot_body_sha256(aid)
            and (t.name or "") == pilot_template_name(aid),
        }
        for t in target_rows
    ]

    return {
        "account_id": aid,
        "target_id": tid,
        "preflight_go": len(blockers) == 0,
        "blockers": blockers,
        "template_audit": template_audit,
    }


def validate_all_pilot_pairs_preflight(
    db: Session,
    pairs: dict[int, int] | None = None,
) -> dict[str, Any]:
    active = dict(pairs or get_campaign_pilot_pairs())
    rows = [
        validate_account_target_preflight(db, aid, active[aid], pairs=active)
        for aid in sorted(active)
    ]
    all_go = all(r.get("preflight_go") for r in rows)
    return {
        "pairs": active,
        "accounts": rows,
        "preflight_go": all_go,
        "outcome": "P9_68_TEMPLATE_PREFLIGHT_GO" if all_go else "P9_68_TEMPLATE_PREFLIGHT_NO_GO",
    }


def apply_template_fix_for_pairs(db: Session, pairs: dict[int, int]) -> dict[str, Any]:
    """Deactivate non-pilot TARGET templates for given pairs (no sends)."""
    from src.core.scheduler_models import AccountTargetBinding

    changes: list[dict[str, Any]] = []
    for aid, tid in sorted(pairs.items()):
        binding = (
            db.query(AccountTargetBinding)
            .filter(
                AccountTargetBinding.account_id == int(aid),
                AccountTargetBinding.target_id == int(tid),
            )
            .first()
        )
        if binding:
            for t in (
                db.query(MessageTemplate)
                .filter(
                    MessageTemplate.binding_id == int(binding.id),
                    MessageTemplate.type == MessageType.PROMO.value,
                    MessageTemplate.scope == "BINDING",
                    MessageTemplate.is_active == True,
                )
                .all()
            ):
                t.is_active = False
                changes.append(
                    {
                        "template_id": int(t.id),
                        "account_id": aid,
                        "action": "deactivated_binding_scope",
                    }
                )
        expected_name = pilot_template_name(aid)
        for t in (
            db.query(MessageTemplate)
            .filter(
                MessageTemplate.target_id == int(tid),
                MessageTemplate.type == MessageType.PROMO.value,
                MessageTemplate.scope == "TARGET",
            )
            .all()
        ):
            is_pilot = (t.name or "") == expected_name and (t.body or "").strip() == approved_pilot_body(
                aid
            ).strip()
            if is_pilot:
                if not t.is_active:
                    t.is_active = True
                t.weight = 10000
                changes.append({"template_id": int(t.id), "action": "activated_pilot"})
                continue
            if t.is_active:
                t.is_active = False
                changes.append(
                    {
                        "template_id": int(t.id),
                        "account_id": aid,
                        "target_id": tid,
                        "name": t.name,
                        "action": "deactivated_non_pilot",
                    }
                )
        pilot = (
            db.query(MessageTemplate)
            .filter(
                MessageTemplate.target_id == int(tid),
                MessageTemplate.name == expected_name,
                MessageTemplate.type == MessageType.PROMO.value,
            )
            .first()
        )
        if not pilot:
            db.add(
                MessageTemplate(
                    type=MessageType.PROMO.value,
                    scope="TARGET",
                    target_id=int(tid),
                    name=expected_name,
                    body=approved_pilot_body(aid),
                    is_active=True,
                    weight=10000,
                )
            )
            changes.append({"account_id": aid, "target_id": tid, "action": "created_pilot_template"})
    db.commit()
    return {"changes": changes, "change_count": len(changes)}
