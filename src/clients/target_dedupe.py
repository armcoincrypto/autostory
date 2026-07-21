"""
Safe duplicate ``ChatTarget`` merge planning and apply (operator tooling).

Never deletes ``MessageDelivery`` rows — repoints ``target_id`` to the canonical target.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Set, Tuple

import structlog

logger = structlog.get_logger(__name__)


def _norm_username(u: Optional[str]) -> Optional[str]:
    if not u or not str(u).strip():
        return None
    return str(u).strip().lstrip("@").lower()


def _norm_invite(v: Optional[Any]) -> Optional[str]:
    if v is None or not str(v).strip():
        return None
    return str(v).strip().lower()


def _binding_count(db: Any, target_id: int) -> int:
    from src.core.scheduler_models import AccountTargetBinding

    return (
        db.query(AccountTargetBinding)
        .filter(AccountTargetBinding.target_id == int(target_id))
        .count()
    )


def _pick_canonical_id(db: Any, ids: List[int]) -> int:
    """Prefer the row with the most bindings; tie-break lower numeric id."""
    best = int(ids[0])
    best_key: Optional[Tuple[int, int]] = None

    def key_for(tid: int) -> Tuple[int, int]:
        c = _binding_count(db, tid)
        return (c, -tid)

    best_key = key_for(best)
    for tid in ids[1:]:
        t = int(tid)
        k = key_for(t)
        if k > best_key:
            best, best_key = t, k
    return best


def _rules_replace_target_id(db: Any, old_id: int, new_id: int) -> int:
    """Replace ``old_id`` with ``new_id`` in ONLY_SELECTED JSON lists. Returns rules touched."""
    from src.core.scheduler_models import ScheduleRule

    updated = 0
    oid, nid = int(old_id), int(new_id)
    for rule in db.query(ScheduleRule).all():
        raw = rule.selected_target_ids_json
        if not raw or not str(raw).strip():
            continue
        try:
            arr = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(arr, list):
            continue
        ch = False
        out: List[int] = []
        for x in arr:
            try:
                v = int(x)
            except (TypeError, ValueError):
                continue
            if v == oid:
                v = nid
                ch = True
            out.append(v)
        if not ch:
            continue
        deduped: List[int] = []
        seen: Set[int] = set()
        for v in out:
            if v not in seen:
                seen.add(v)
                deduped.append(v)
        rule.selected_target_ids_json = json.dumps(deduped)
        updated += 1
    return updated


def build_plan_for_target_ids(db: Any, target_ids: List[int], *, group_key: str) -> Dict[str, Any]:
    from src.core.scheduler_models import (
        AccountTargetBinding,
        AccountTargetMembershipProbe,
        ChatTarget,
        MessageDelivery,
        MessageTemplate,
        ScheduledJob,
    )

    ids = sorted({int(x) for x in target_ids})
    if len(ids) < 2:
        return {
            "group_key": group_key,
            "error": "need at least two target ids in a duplicate group",
            "canonical_target_id": None,
            "duplicate_target_ids": [],
            "bindings_to_move": [],
            "blockers": [],
        }

    rows = {t.id: t for t in db.query(ChatTarget).filter(ChatTarget.id.in_(ids)).all()}
    if len(rows) != len(ids):
        return {
            "group_key": group_key,
            "error": "one or more target ids not found",
            "canonical_target_id": None,
            "duplicate_target_ids": ids,
            "bindings_to_move": [],
            "blockers": [],
        }

    structural: List[Dict[str, Any]] = []

    # Same tg_id cluster but divergent @handles — unsafe to auto-merge.
    if group_key.startswith("tg:"):
        unames = {_norm_username(getattr(t, "username", None)) for t in rows.values()}
        unames.discard(None)
        if len(unames) > 1:
            structural.append({
                "code": "tgid_username_fork",
                "detail": sorted(unames),
            })

    # Username-key group: rows must share one normalized username.
    if not group_key.startswith("tg:"):
        keys: Set[Optional[str]] = set()
        for t in rows.values():
            keys.add(_norm_username(getattr(t, "username", None)))
        keys.discard(None)
        if len(keys) > 1:
            structural.append({
                "code": "username_mismatch",
                "detail": sorted(keys),
            })

    invites = {_norm_invite(getattr(t, "invite_link", None)) for t in rows.values()}
    invites.discard(None)
    if len(invites) > 1:
        structural.append({
            "code": "invite_fork",
            "detail": sorted(invites),
        })

    canonical_id = _pick_canonical_id(db, ids)
    dup_ids = [i for i in ids if i != canonical_id]

    bindings_to_move: List[Dict[str, Any]] = []
    binding_blockers: List[Dict[str, Any]] = []
    canon_bind_accounts = {
        b.account_id
        for b in db.query(AccountTargetBinding)
        .filter(AccountTargetBinding.target_id == canonical_id)
        .all()
    }

    for did in dup_ids:
        for b in (
            db.query(AccountTargetBinding)
            .filter(AccountTargetBinding.target_id == did)
            .all()
        ):
            if b.account_id in canon_bind_accounts:
                binding_blockers.append({
                    "code": "binding_conflict",
                    "detail": (
                        f"account {b.account_id} already bound on canonical {canonical_id} "
                        f"and duplicate {did}"
                    ),
                    "binding_id": b.id,
                })
            else:
                bindings_to_move.append({
                    "binding_id": b.id,
                    "account_id": b.account_id,
                    "from_target_id": did,
                    "to_target_id": canonical_id,
                })

    tmpl_n = (
        db.query(MessageTemplate)
        .filter(MessageTemplate.target_id.in_(dup_ids))
        .count()
    )
    job_n = (
        db.query(ScheduledJob)
        .filter(ScheduledJob.target_id.in_(dup_ids))
        .count()
    )
    deliv_n = (
        db.query(MessageDelivery)
        .filter(MessageDelivery.target_id.in_(dup_ids))
        .count()
    )
    probe_n = (
        db.query(AccountTargetMembershipProbe)
        .filter(AccountTargetMembershipProbe.target_id.in_(dup_ids))
        .count()
    )

    blockers = structural + binding_blockers
    safe = not blockers and tmpl_n == 0 and job_n == 0

    return {
        "group_key": group_key,
        "canonical_target_id": canonical_id,
        "duplicate_target_ids": dup_ids,
        "bindings_to_move": bindings_to_move,
        "blockers": blockers,
        "templates_pointing_at_duplicates": int(tmpl_n),
        "scheduled_jobs_pointing_at_duplicates": int(job_n),
        "deliveries_pointing_at_duplicates": int(deliv_n),
        "membership_probes_on_duplicates": int(probe_n),
        "safe_to_delete_duplicates_after_apply": bool(safe),
    }


def dry_run_all_duplicate_groups(db: Any) -> Dict[str, Any]:
    from src.clients.readiness_store import detect_duplicate_targets

    rep = detect_duplicate_targets(db)
    plans: List[Dict[str, Any]] = []
    for g in rep.get("duplicate_username_groups") or []:
        tid = g.get("target_ids") or []
        plans.append(build_plan_for_target_ids(db, tid, group_key="@" + str(g.get("username_key"))))
    for g in rep.get("duplicate_tg_id_groups") or []:
        tid = g.get("target_ids") or []
        plans.append(build_plan_for_target_ids(db, tid, group_key="tg:" + str(g.get("tg_id"))))
    return {
        "has_duplicates": bool(rep.get("has_duplicates")),
        "duplicate_username_groups": rep.get("duplicate_username_groups"),
        "duplicate_tg_id_groups": rep.get("duplicate_tg_id_groups"),
        "plans": plans,
    }


def apply_dedupe_merge(
    db: Any,
    *,
    canonical_target_id: int,
    duplicate_target_ids: List[int],
    dry_run: bool = True,
) -> Tuple[Dict[str, Any], int]:
    """
    Move bindings from duplicates to canonical, repoint templates/jobs/deliveries,
    delete duplicate ``ChatTarget`` rows only when unreferenced.
    """
    from sqlalchemy.exc import IntegrityError

    from src.core.scheduler_models import (
        AccountTargetBinding,
        AccountTargetMembershipProbe,
        ChatTarget,
        MessageDelivery,
        MessageTemplate,
        ScheduledJob,
    )

    cid = int(canonical_target_id)
    dup_ids = sorted({int(x) for x in duplicate_target_ids if int(x) != cid})
    if not dup_ids:
        return {"error": "no duplicate ids to merge", "dry_run": dry_run}, 400

    canon = db.query(ChatTarget).filter(ChatTarget.id == cid).first()
    if not canon:
        return {"error": "canonical target not found", "dry_run": dry_run}, 404

    plan = build_plan_for_target_ids(db, [cid] + dup_ids, group_key=f"apply:{cid}")
    if plan.get("error"):
        return {"error": plan["error"], "plan": plan, "dry_run": dry_run}, 400
    if plan.get("blockers"):
        return {
            "error": "unsafe merge: resolve structural or binding conflicts first",
            "blockers": plan["blockers"],
            "plan": plan,
            "dry_run": dry_run,
        }, 409

    if dry_run:
        return {
            "dry_run": True,
            "would_apply": plan,
            "canonical_target_id": cid,
            "duplicate_target_ids": dup_ids,
        }, 200

    moved_bindings = 0
    try:
        for b in (
            db.query(AccountTargetBinding)
            .filter(AccountTargetBinding.target_id.in_(dup_ids))
            .all()
        ):
            clash = (
                db.query(AccountTargetBinding)
                .filter(
                    AccountTargetBinding.target_id == cid,
                    AccountTargetBinding.account_id == b.account_id,
                )
                .first()
            )
            if clash:
                return {
                    "error": "binding conflict during apply",
                    "account_id": b.account_id,
                    "dry_run": False,
                }, 409
            b.target_id = cid
            moved_bindings += 1

        db.query(MessageTemplate).filter(MessageTemplate.target_id.in_(dup_ids)).update(
            {MessageTemplate.target_id: cid},
            synchronize_session=False,
        )
        db.query(ScheduledJob).filter(ScheduledJob.target_id.in_(dup_ids)).update(
            {ScheduledJob.target_id: cid},
            synchronize_session=False,
        )
        db.query(MessageDelivery).filter(MessageDelivery.target_id.in_(dup_ids)).update(
            {MessageDelivery.target_id: cid},
            synchronize_session=False,
        )
        db.query(AccountTargetMembershipProbe).filter(
            AccountTargetMembershipProbe.target_id.in_(dup_ids)
        ).delete(synchronize_session=False)

        rules_updated = 0
        for did in dup_ids:
            rules_updated += _rules_replace_target_id(db, did, cid)

        # Ensure bulk updates are visible to follow-up existence checks (SQLite).
        db.flush()

        deleted_targets: List[int] = []
        for did in dup_ids:
            still = (
                db.query(AccountTargetBinding)
                .filter(AccountTargetBinding.target_id == did)
                .count()
            )
            if still:
                continue
            t = db.query(ChatTarget).filter(ChatTarget.id == did).first()
            if t:
                db.delete(t)
                deleted_targets.append(int(did))

        logger.info(
            "target_dedupe_apply",
            canonical=cid,
            duplicates=dup_ids,
            moved_bindings=moved_bindings,
            deleted_targets=deleted_targets,
            rules_updated=rules_updated,
        )
        return {
            "success": True,
            "dry_run": False,
            "canonical_target_id": cid,
            "duplicate_target_ids": dup_ids,
            "moved_bindings": moved_bindings,
            "deleted_chat_target_ids": deleted_targets,
            "schedule_rules_json_lists_updated": rules_updated,
        }, 200
    except IntegrityError as e:
        logger.warning("target_dedupe_integrity_error", error=str(e))
        return {
            "error": "integrity error during apply (concurrent change?)",
            "detail": str(e),
            "dry_run": False,
        }, 409
