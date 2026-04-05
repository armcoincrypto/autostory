"""
Live identity audit: compare DB Account row to Telegram get_me() after a successful check.
Operator-only hints; never auto-delete or auto-change account state (callers persist audit fields only).
"""
from __future__ import annotations

from typing import Any, Optional, Tuple


def _digits(s: Optional[str]) -> str:
    if not s:
        return ""
    return "".join(c for c in str(s) if c.isdigit())


def _uname(s: Optional[str]) -> str:
    if not s:
        return ""
    return str(s).strip().lstrip("@").lower()


def _name(s: Optional[str]) -> str:
    if not s:
        return ""
    return str(s).strip().lower()


def classify_identity_from_me(account: Any, me: Any) -> Tuple[str, str]:
    """
    Requires an authenticated get_me() User. Returns (identity_status, identity_reason).

    identity_ok — DB aligns with get_me (same telegram id; profile fields match or not contradicted).
    metadata_stale — Same telegram user (user_id matches) but phone/username/name differ from DB.
    identity_mismatch — DB user_id conflicts with session user, or phone contradicts session with no link.
    """
    me_id = getattr(me, "id", None)
    if me_id is None:
        return "session_invalid", "get_me_missing_id"

    db_uid = getattr(account, "user_id", None)
    if db_uid is not None and int(db_uid) != int(me_id):
        return "identity_mismatch", f"db_user_id_{db_uid}_session_{me_id}"

    me_phone = _digits(getattr(me, "phone", None))
    db_phone = _digits(getattr(account, "phone_number", None))
    me_un = _uname(getattr(me, "username", None))
    db_un = _uname(getattr(account, "username", None))
    me_fn = _name(getattr(me, "first_name", None))
    db_fn = _name(getattr(account, "first_name", None))
    me_ln = _name(getattr(me, "last_name", None))
    db_ln = _name(getattr(account, "last_name", None))

    if db_uid is None:
        if me_phone and db_phone and len(me_phone) >= 10 and len(db_phone) >= 10:
            if me_phone[-10:] != db_phone[-10:]:
                return "identity_mismatch", "phone_differs_no_user_id_link"
        stale_nid: list[str] = []
        if me_un and not db_un:
            stale_nid.append("username_missing_in_db")
        elif db_un and not me_un:
            stale_nid.append("username_cleared_on_telegram")
        if me_fn and not db_fn:
            stale_nid.append("first_name_missing_in_db")
        if me_ln and not db_ln:
            stale_nid.append("last_name_missing_in_db")
        if me_phone and len(me_phone) >= 8 and (not db_phone or len(db_phone) < 8):
            stale_nid.append("phone_missing_in_db")
        if stale_nid:
            return "metadata_stale", ",".join(stale_nid)
        return "identity_ok", "session_user_not_linked_by_id"

    stale: list[str] = []
    if me_phone and db_phone and len(me_phone) >= 8 and len(db_phone) >= 8:
        if me_phone != db_phone and me_phone[-10:] != db_phone[-10:]:
            stale.append("phone")
    elif me_phone and len(me_phone) >= 8 and (not db_phone or len(db_phone) < 8):
        stale.append("phone_missing_in_db")
    if me_un and db_un and me_un != db_un:
        stale.append("username")
    elif me_un and not db_un:
        stale.append("username_missing_in_db")
    elif db_un and not me_un:
        stale.append("username_cleared_on_telegram")
    if me_fn and db_fn and me_fn != db_fn:
        stale.append("first_name")
    elif me_fn and not db_fn:
        stale.append("first_name_missing_in_db")
    if me_ln and db_ln and me_ln != db_ln:
        stale.append("last_name")
    elif me_ln and not db_ln:
        stale.append("last_name_missing_in_db")

    if stale:
        return "metadata_stale", ",".join(stale)

    return "identity_ok", "aligned_with_get_me"


def attach_identity_to_check_result(
    out: dict,
    account_or_none: Any,
    me_or_none: Any,
) -> None:
    """Mutates out with identity_status, identity_reason, identity_verify_suggested."""
    status = out.get("status")
    if status == "alive":
        if account_or_none is not None and me_or_none is not None:
            id_st, id_re = classify_identity_from_me(account_or_none, me_or_none)
        else:
            id_st, id_re = "unknown", "alive_missing_account_or_me"
    else:
        id_st = "session_invalid"
        id_re = (out.get("reason_code") or out.get("status") or "check_failed")[:250]

    out["identity_status"] = id_st
    out["identity_reason"] = id_re[:255]
    # Operators: only flag actionable identity/session audit states (not "unknown" edge cases).
    out["identity_verify_suggested"] = id_st in ("metadata_stale", "identity_mismatch", "session_invalid")


def persist_identity_audit_fields(acc: Any, out: dict, now_utc: Any) -> None:
    if not acc or "identity_status" not in out:
        return
    acc.identity_audit_status = out["identity_status"]
    acc.identity_audit_reason = (out.get("identity_reason") or "")[:255]
    acc.identity_audit_at = now_utc
