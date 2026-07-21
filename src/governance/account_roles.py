"""DB-backed account runtime roles and tags."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from src.core.database import get_db_context
from src.governance.constants import ALL_ROLES
from src.governance.fallback import fallback_roles_for_account
from src.governance.models import AccountCohort, AccountCohortMember, AccountPinned, AccountRuntimeRole, AccountRuntimeTag


def _normalize_role(role: str) -> str:
    value = (role or "").strip().upper()
    if value not in ALL_ROLES:
        raise ValueError(f"Unknown role: {role}")
    return value


def _normalize_tag(tag: str) -> str:
    value = (tag or "").strip().upper().replace(" ", "_")
    if not value or len(value) > 64:
        raise ValueError("Invalid tag")
    return value


def get_db_roles(db: Session, account_id: int, *, active_only: bool = True) -> list[str]:
    q = db.query(AccountRuntimeRole).filter(AccountRuntimeRole.account_id == int(account_id))
    if active_only:
        q = q.filter(AccountRuntimeRole.active.is_(True))
    return sorted({(row.role or "").strip().upper() for row in q.all() if row.role})


def get_db_tags(db: Session, account_id: int, *, active_only: bool = True) -> list[str]:
    q = db.query(AccountRuntimeTag).filter(AccountRuntimeTag.account_id == int(account_id))
    if active_only:
        q = q.filter(AccountRuntimeTag.active.is_(True))
    return sorted({(row.tag or "").strip().upper() for row in q.all() if row.tag})


def get_account_roles(account_id: int, db: Session | None = None) -> list[str]:
    """Effective roles = DB roles ∪ hardcoded fallback (fallback cannot be removed)."""
    if db is not None:
        db_roles = set(get_db_roles(db, account_id))
        return sorted(db_roles | fallback_roles_for_account(account_id))

    with get_db_context() as session:
        db_roles = set(get_db_roles(session, account_id))
        return sorted(db_roles | fallback_roles_for_account(account_id))


def has_role(account_id: int, role: str, db: Session | None = None) -> bool:
    role_norm = _normalize_role(role)
    return role_norm in get_account_roles(account_id, db=db)


def add_role(
    account_id: int,
    role: str,
    *,
    reason: str | None = None,
    created_by: str | None = None,
    db: Session | None = None,
) -> dict[str, Any]:
    role_norm = _normalize_role(role)

    def _apply(session: Session) -> dict[str, Any]:
        row = (
            session.query(AccountRuntimeRole)
            .filter(
                AccountRuntimeRole.account_id == int(account_id),
                AccountRuntimeRole.role == role_norm,
            )
            .first()
        )
        if row is None:
            row = AccountRuntimeRole(
                account_id=int(account_id),
                role=role_norm,
                reason=(reason or "").strip() or None,
                created_by=(created_by or "").strip() or None,
                active=True,
                created_at=datetime.utcnow(),
            )
            session.add(row)
        else:
            row.active = True
            row.reason = (reason or row.reason or "").strip() or None
            row.created_by = (created_by or row.created_by or "").strip() or None
        return {"account_id": int(account_id), "role": role_norm, "active": True}

    if db is not None:
        return _apply(db)
    with get_db_context() as session:
        result = _apply(session)
        session.commit()
        return result


def remove_role(
    account_id: int,
    role: str,
    *,
    db: Session | None = None,
) -> dict[str, Any]:
    role_norm = _normalize_role(role)
    if role_norm in fallback_roles_for_account(account_id):
        return {
            "account_id": int(account_id),
            "role": role_norm,
            "active": True,
            "removed": False,
            "note": "fallback_role_cannot_be_removed_from_db",
        }

    def _apply(session: Session) -> dict[str, Any]:
        row = (
            session.query(AccountRuntimeRole)
            .filter(
                AccountRuntimeRole.account_id == int(account_id),
                AccountRuntimeRole.role == role_norm,
            )
            .first()
        )
        if row is not None:
            row.active = False
        return {"account_id": int(account_id), "role": role_norm, "active": False, "removed": True}

    if db is not None:
        return _apply(db)
    with get_db_context() as session:
        result = _apply(session)
        session.commit()
        return result


def get_accounts_with_role(role: str, db: Session | None = None) -> list[int]:
    role_norm = _normalize_role(role)

    def _apply(session: Session) -> list[int]:
        return sorted(
            {
                int(row.account_id)
                for row in session.query(AccountRuntimeRole)
                .filter(AccountRuntimeRole.role == role_norm, AccountRuntimeRole.active.is_(True))
                .all()
            }
        )

    if db is not None:
        return _apply(db)
    with get_db_context() as session:
        return _apply(session)


def add_tag(
    account_id: int,
    tag: str,
    *,
    created_by: str | None = None,
    db: Session | None = None,
) -> dict[str, Any]:
    tag_norm = _normalize_tag(tag)

    def _apply(session: Session) -> dict[str, Any]:
        row = (
            session.query(AccountRuntimeTag)
            .filter(
                AccountRuntimeTag.account_id == int(account_id),
                AccountRuntimeTag.tag == tag_norm,
            )
            .first()
        )
        if row is None:
            row = AccountRuntimeTag(
                account_id=int(account_id),
                tag=tag_norm,
                created_by=(created_by or "").strip() or None,
                active=True,
                created_at=datetime.utcnow(),
            )
            session.add(row)
        else:
            row.active = True
        return {"account_id": int(account_id), "tag": tag_norm, "active": True}

    if db is not None:
        return _apply(db)
    with get_db_context() as session:
        result = _apply(session)
        session.commit()
        return result


def remove_tag(account_id: int, tag: str, *, db: Session | None = None) -> dict[str, Any]:
    tag_norm = _normalize_tag(tag)

    def _apply(session: Session) -> dict[str, Any]:
        row = (
            session.query(AccountRuntimeTag)
            .filter(
                AccountRuntimeTag.account_id == int(account_id),
                AccountRuntimeTag.tag == tag_norm,
            )
            .first()
        )
        if row is not None:
            row.active = False
        return {"account_id": int(account_id), "tag": tag_norm, "active": False}

    if db is not None:
        return _apply(db)
    with get_db_context() as session:
        result = _apply(session)
        session.commit()
        return result


def list_pinned_account_ids(db: Session) -> list[int]:
    return sorted(
        int(row.account_id)
        for row in db.query(AccountPinned)
        .filter(AccountPinned.active.is_(True))
        .order_by(AccountPinned.pinned_at.desc())
        .all()
    )


def set_account_pinned(
    account_id: int,
    *,
    pinned: bool,
    note: str | None = None,
    pinned_by: str | None = None,
    db: Session | None = None,
) -> dict[str, Any]:
    def _apply(session: Session) -> dict[str, Any]:
        row = session.query(AccountPinned).filter(AccountPinned.account_id == int(account_id)).first()
        if pinned:
            if row is None:
                row = AccountPinned(account_id=int(account_id), note=note, pinned_by=pinned_by, active=True)
                session.add(row)
            else:
                row.active = True
                row.note = note or row.note
                row.pinned_by = pinned_by or row.pinned_by
        elif row is not None:
            row.active = False
        return {"account_id": int(account_id), "pinned": bool(pinned)}

    if db is not None:
        return _apply(db)
    with get_db_context() as session:
        result = _apply(session)
        session.commit()
        return result


def list_cohorts(db: Session, *, active_only: bool = True) -> list[dict[str, Any]]:
    q = db.query(AccountCohort).order_by(AccountCohort.slug.asc())
    if active_only:
        q = q.filter(AccountCohort.active.is_(True))
    return [
        {
            "id": int(c.id),
            "slug": c.slug,
            "name": c.name,
            "description": c.description,
            "member_count": int(
                db.query(AccountCohortMember)
                .filter(AccountCohortMember.cohort_id == c.id, AccountCohortMember.active.is_(True))
                .count()
            ),
        }
        for c in q.all()
    ]


def get_cohort_member_ids(db: Session, cohort_id: int) -> list[int]:
    return sorted(
        int(row.account_id)
        for row in db.query(AccountCohortMember)
        .filter(AccountCohortMember.cohort_id == int(cohort_id), AccountCohortMember.active.is_(True))
        .all()
    )


def upsert_cohort(
    db: Session,
    *,
    slug: str,
    name: str,
    description: str | None = None,
    created_by: str | None = None,
) -> AccountCohort:
    slug_norm = (slug or "").strip().upper().replace(" ", "_")
    row = db.query(AccountCohort).filter(AccountCohort.slug == slug_norm).first()
    if row is None:
        row = AccountCohort(slug=slug_norm, name=name.strip(), description=description, created_by=created_by)
        db.add(row)
        db.flush()
    else:
        row.name = name.strip()
        row.description = description
        row.active = True
    return row


def set_cohort_members(
    db: Session,
    cohort_id: int,
    account_ids: list[int],
    *,
    added_by: str | None = None,
) -> None:
    desired = {int(x) for x in account_ids}
    existing = db.query(AccountCohortMember).filter(AccountCohortMember.cohort_id == int(cohort_id)).all()
    for row in existing:
        aid = int(row.account_id)
        if aid in desired:
            row.active = True
            desired.discard(aid)
        else:
            row.active = False
    for aid in sorted(desired):
        db.add(
            AccountCohortMember(
                cohort_id=int(cohort_id),
                account_id=aid,
                added_by=added_by,
                active=True,
            )
        )
