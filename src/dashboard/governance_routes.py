"""P10.21 governance API — roles, tags, cohorts, eligibility preview (JSON only)."""
from __future__ import annotations

from flask import Blueprint, jsonify, request
from flask_login import current_user

from src.core.database import get_db_context
from src.core.models import Account
from src.dashboard.auth_access import dashboard_api_authorized
from src.governance.account_roles import (
    add_role,
    add_tag,
    get_account_roles,
    get_db_tags,
    list_cohorts,
    list_pinned_account_ids,
    remove_role,
    remove_tag,
    set_account_pinned,
    set_cohort_members,
    upsert_cohort,
)
from src.governance.audit import log_governance_action
from src.governance.constants import ROLES_REQUIRING_REASON
from src.governance.governance_resolver import build_execution_eligibility_preview, resolve_account_governance

governance_api = Blueprint("governance_api", __name__, url_prefix="/api")


def _require_admin_json():
    if not dashboard_api_authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    return None


def _actor_label() -> str:
    if current_user.is_authenticated:
        return str(getattr(current_user, "email", None) or getattr(current_user, "id", "dashboard_user"))
    return "admin_token"


@governance_api.route("/accounts/<int:account_id>/governance", methods=["GET"])
def account_governance_detail(account_id: int):
    denied = _require_admin_json()
    if denied:
        return denied
    with get_db_context() as db:
        account = db.get(Account, int(account_id))
        if not account:
            return jsonify({"ok": False, "error": "account_not_found"}), 404
        gov = resolve_account_governance(db, account)
        gov["roles_list"] = get_account_roles(account_id, db=db)
        gov["tags_list"] = get_db_tags(db, account_id)
        gov["pinned"] = int(account_id) in list_pinned_account_ids(db)
    return jsonify({"ok": True, "governance": gov})


@governance_api.route("/accounts/<int:account_id>/roles", methods=["GET", "POST", "DELETE"])
def account_roles_api(account_id: int):
    denied = _require_admin_json()
    if denied:
        return denied
    if request.method == "GET":
        with get_db_context() as db:
            roles = get_account_roles(account_id, db=db)
        return jsonify({"ok": True, "account_id": account_id, "roles": roles})

    payload = request.get_json(silent=True) or {}
    role = (payload.get("role") or request.args.get("role") or "").strip()
    if not role:
        return jsonify({"ok": False, "error": "role_required"}), 400
    reason = (payload.get("reason") or "").strip()
    if role.upper() in ROLES_REQUIRING_REASON and not reason:
        return jsonify({"ok": False, "error": "reason_required_for_role", "role": role.upper()}), 400

    actor = _actor_label()
    if request.method == "POST":
        with get_db_context() as db:
            result = add_role(account_id, role, reason=reason, created_by=actor, db=db)
            db.commit()
        log_governance_action(
            account_id=account_id,
            action="role_added",
            target_type="role",
            target_value=role.upper(),
            reason=reason,
            actor=actor,
        )
        return jsonify({"ok": True, **result})

    with get_db_context() as db:
        result = remove_role(account_id, role, db=db)
        db.commit()
    log_governance_action(
        account_id=account_id,
        action="role_removed",
        target_type="role",
        target_value=role.upper(),
        reason=reason or None,
        actor=actor,
    )
    return jsonify({"ok": True, **result})


@governance_api.route("/accounts/<int:account_id>/tags", methods=["GET", "POST", "DELETE"])
def account_tags_api(account_id: int):
    denied = _require_admin_json()
    if denied:
        return denied
    if request.method == "GET":
        with get_db_context() as db:
            tags = get_db_tags(db, account_id)
        return jsonify({"ok": True, "account_id": account_id, "tags": tags})

    payload = request.get_json(silent=True) or {}
    tag = (payload.get("tag") or request.args.get("tag") or "").strip()
    if not tag:
        return jsonify({"ok": False, "error": "tag_required"}), 400
    actor = _actor_label()
    if request.method == "POST":
        with get_db_context() as db:
            result = add_tag(account_id, tag, created_by=actor, db=db)
            db.commit()
        log_governance_action(
            account_id=account_id,
            action="tag_added",
            target_type="tag",
            target_value=tag.upper(),
            actor=actor,
        )
        return jsonify({"ok": True, **result})

    with get_db_context() as db:
        result = remove_tag(account_id, tag, db=db)
        db.commit()
    log_governance_action(
        account_id=account_id,
        action="tag_removed",
        target_type="tag",
        target_value=tag.upper(),
        actor=actor,
    )
    return jsonify({"ok": True, **result})


@governance_api.route("/accounts/<int:account_id>/pin", methods=["POST", "DELETE"])
def account_pin_api(account_id: int):
    denied = _require_admin_json()
    if denied:
        return denied
    actor = _actor_label()
    payload = request.get_json(silent=True) or {}
    pinned = request.method == "POST"
    with get_db_context() as db:
        result = set_account_pinned(
            account_id,
            pinned=pinned,
            note=payload.get("note"),
            pinned_by=actor,
            db=db,
        )
        db.commit()
    log_governance_action(
        account_id=account_id,
        action="account_pinned" if pinned else "account_unpinned",
        target_type="pin",
        target_value=str(account_id),
        actor=actor,
    )
    return jsonify({"ok": True, **result})


@governance_api.route("/governance/eligibility-preview", methods=["GET", "POST"])
def governance_eligibility_preview():
    denied = _require_admin_json()
    if denied:
        return denied
    payload = request.get_json(silent=True) if request.method == "POST" else {}
    payload = payload or {}
    module = request.args.get("module") or payload.get("module") or "stories"
    account_ids = payload.get("account_ids")
    cohort_slug = (request.args.get("cohort") or payload.get("cohort") or "").strip().upper()
    with get_db_context() as db:
        ids = None
        if cohort_slug:
            from src.governance.models import AccountCohort

            cohort = db.query(AccountCohort).filter(AccountCohort.slug == cohort_slug, AccountCohort.active.is_(True)).first()
            if cohort:
                from src.governance.account_roles import get_cohort_member_ids

                ids = get_cohort_member_ids(db, int(cohort.id))
        if account_ids:
            ids = [int(x) for x in account_ids]
        preview = build_execution_eligibility_preview(db, account_ids=ids, module=module)
        preview["pinned_account_ids"] = list_pinned_account_ids(db)
        preview["cohorts"] = list_cohorts(db)
    return jsonify({"ok": True, "preview": preview})


@governance_api.route("/governance/cohorts", methods=["GET", "POST"])
def governance_cohorts_api():
    denied = _require_admin_json()
    if denied:
        return denied
    if request.method == "GET":
        with get_db_context() as db:
            cohorts = list_cohorts(db)
        return jsonify({"ok": True, "cohorts": cohorts})

    payload = request.get_json(silent=True) or {}
    slug = (payload.get("slug") or "").strip()
    name = (payload.get("name") or slug).strip()
    if not slug:
        return jsonify({"ok": False, "error": "slug_required"}), 400
    actor = _actor_label()
    with get_db_context() as db:
        cohort = upsert_cohort(db, slug=slug, name=name, description=payload.get("description"), created_by=actor)
        member_ids = payload.get("account_ids") or []
        if member_ids:
            set_cohort_members(db, int(cohort.id), [int(x) for x in member_ids], added_by=actor)
        db.commit()
        cohorts = list_cohorts(db)
    log_governance_action(
        account_id=None,
        action="cohort_upserted",
        target_type="cohort",
        target_value=slug.upper(),
        actor=actor,
        payload={"name": name, "member_count": len(member_ids)},
    )
    return jsonify({"ok": True, "cohorts": cohorts})
