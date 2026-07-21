"""
P9.1 — Read-only account operational state (no Telethon open, no session writes).

Computes a single operator-facing view from DB rows + shallow file/PRAGMA inspection.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.orm import Session

from src.ai_agent.account_allowlist import (
    RESERVED_AI_AGENT_ACCOUNT_IDS,
    account_id_excluded_from_scheduler_worker,
)
from src.clients.session_resolve import (
    ERR_EMPTY_SESSION,
    ERR_INVALID_SESSION_FORMAT,
    ERR_LEGACY_SQLITE_SESSION_FORMAT,
    ERR_SESSION_FILE_MISSING,
    _looks_like_filesystem_path,
    _strip_file_url,
    probe_telethon_session_kind,
)
from src.core.models import Account, AccountStatus
from src.core.scheduler_models import AccountReadinessSnapshot
from src.core.session_paths import get_canonical_session_path
from src.core.telethon_schema import TELETHON_COMPATIBLE_SCHEMA_VERSION

# Dexpert / controller lines — operator-critical; not in RESERVED_AI_AGENT_ACCOUNT_IDS.
CONTROLLER_ACCOUNT_IDS: frozenset[int] = frozenset({206, 207})

_PURPOSE_SCHEDULER_OK = frozenset({"both", "autostory", "messaging", ""})
_PURPOSE_DISCOVERY_OK = frozenset({"both", "autostory", "messaging", ""})


def _account_label(account: Account) -> str:
    un = (getattr(account, "username", None) or "").strip()
    if un:
        return un if un.startswith("@") else f"@{un}"
    phone = (getattr(account, "phone_number", None) or "").strip()
    if phone:
        return phone
    first = (getattr(account, "first_name", None) or "").strip()
    last = (getattr(account, "last_name", None) or "").strip()
    name = " ".join(x for x in (first, last) if x)
    if name:
        return name
    return f"account_{int(account.id)}"


def _resolve_session_path(account: Account) -> tuple[bool, Optional[str]]:
    """First existing session file path (same probe order as session_resolve)."""
    ss = (getattr(account, "session_string", None) or "").strip()
    sp_attr = getattr(account, "session_path", None)
    sp = (sp_attr or "").strip() if sp_attr else ""
    account_id = getattr(account, "id", None)

    paths: list[Path] = []
    if ss:
        raw = _strip_file_url(ss)
        try:
            paths.append(Path(raw).expanduser())
        except Exception:
            pass
    if sp:
        try:
            paths.append(Path(_strip_file_url(sp)).expanduser().resolve())
        except Exception:
            pass
    if account_id is not None:
        try:
            paths.append(get_canonical_session_path(int(account_id)))
        except Exception:
            pass

    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            if path.is_file():
                return True, str(path.resolve())
        except OSError:
            continue
    return False, None


def _sqlite_schema_version_readonly(path: Path) -> tuple[Optional[int], Optional[int], list[str]]:
    """
  Read Telethon session DB metadata without Telethon.

  Returns (db_version from version table, sessions_column_count, warnings).
    """
    warnings: list[str] = []
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as e:
        warnings.append(f"sqlite_open_failed:{type(e).__name__}")
        return None, None, warnings
    try:
        ver_row = con.execute("SELECT version FROM version LIMIT 1").fetchone()
        db_version = int(ver_row[0]) if ver_row and ver_row[0] is not None else None
        cols = [r[1] for r in con.execute("PRAGMA table_info(sessions)").fetchall()]
        col_count = len(cols) if cols else None
        table_names = [
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        if "sessions" not in table_names:
            warnings.append("missing_sessions_table")
        return db_version, col_count, warnings
    except sqlite3.Error as e:
        warnings.append(f"sqlite_read_failed:{type(e).__name__}")
        return None, None, warnings
    finally:
        con.close()


def _infer_resolver_code(
    account: Account,
    *,
    session_kind: str,
    probe_error: Optional[str],
    session_exists: bool,
    session_path: Optional[str],
) -> Optional[str]:
    """Machine resolver code; None means ok."""
    if probe_error:
        return probe_error
    if session_kind == "empty":
        return ERR_EMPTY_SESSION
    if session_kind == "string":
        ss = (getattr(account, "session_string", None) or "").strip()
        if not ss:
            return ERR_EMPTY_SESSION
        if _looks_like_filesystem_path(_strip_file_url(ss)) and not session_exists:
            return ERR_SESSION_FILE_MISSING
        return None
    if session_kind == "file":
        if not session_exists or not session_path:
            return ERR_SESSION_FILE_MISSING
        db_ver, col_count, _w = _sqlite_schema_version_readonly(Path(session_path))
        effective_ver: Optional[int] = db_ver
        if effective_ver is None and col_count is not None:
            effective_ver = 8 if col_count >= 6 else 7 if col_count == 5 else None
        compat = int(TELETHON_COMPATIBLE_SCHEMA_VERSION)
        if effective_ver is None:
            return ERR_LEGACY_SQLITE_SESSION_FORMAT
        if int(effective_ver) != compat:
            return ERR_LEGACY_SQLITE_SESSION_FORMAT
        return None
    return ERR_INVALID_SESSION_FORMAT if session_kind == "unknown" else None


def _tier_for(account_id: int) -> str:
    if int(account_id) in RESERVED_AI_AGENT_ACCOUNT_IDS:
        return "reserved"
    if int(account_id) in CONTROLLER_ACCOUNT_IDS:
        return "controller"
    return "fleet"


def _status_value(account: Account) -> str:
    st = getattr(account, "status", None)
    if st is None:
        return ""
    return st.value if hasattr(st, "value") else str(st)


def _readiness_fields(db: Session, account_id: int) -> tuple[Optional[str], Optional[str], Optional[str]]:
    snap = (
        db.query(AccountReadinessSnapshot)
        .filter(AccountReadinessSnapshot.account_id == int(account_id))
        .first()
    )
    if snap is None:
        return None, None, None
    status = (snap.status or "").strip() or None
    failure = (snap.failure_code or "").strip() or None
    checked = snap.checked_at
    checked_iso = None
    if checked is not None:
        if getattr(checked, "tzinfo", None) is None:
            checked = checked.replace(tzinfo=timezone.utc)
        checked_iso = checked.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return status, failure, checked_iso


def compute_account_operational_state(db: Session, account: Account) -> dict[str, Any]:
    """Build one operational-state row for API/CLI (read-only)."""
    aid = int(account.id)
    tier = _tier_for(aid)
    warnings: list[str] = []

    if tier == "reserved":
        warnings.append("reserved_account_immutable")
    elif tier == "controller":
        warnings.append("controller_account_operator_critical")

    session_kind, probe_err = probe_telethon_session_kind(account)
    session_exists, session_path = _resolve_session_path(account)

    schema_version: Optional[int] = None
    if session_exists and session_path and session_kind == "file":
        db_ver, col_count, sqlite_warn = _sqlite_schema_version_readonly(Path(session_path))
        warnings.extend(sqlite_warn)
        if db_ver is not None:
            schema_version = db_ver
        elif col_count is not None:
            schema_version = 8 if col_count >= 6 else 7 if col_count == 5 else None

    resolver_code = _infer_resolver_code(
        account,
        session_kind=session_kind,
        probe_error=probe_err,
        session_exists=session_exists,
        session_path=session_path,
    )
    if resolver_code == ERR_LEGACY_SQLITE_SESSION_FORMAT:
        warnings.append("telethon_sqlite_schema_v8_incompatible_with_runtime_1_42")

    readiness_status, readiness_failure, readiness_checked = _readiness_fields(db, aid)
    health_status = (getattr(account, "health_status", None) or "").strip() or None
    health_checked = getattr(account, "health_checked_at", None)
    health_iso = None
    if health_checked is not None:
        if getattr(health_checked, "tzinfo", None) is None:
            health_checked = health_checked.replace(tzinfo=timezone.utc)
        health_iso = health_checked.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    last_resolved_at = readiness_checked or health_iso

    purpose = (getattr(account, "purpose", None) or "both").strip().lower() or "both"
    status = _status_value(account).lower()

    scheduler_eligible = False
    discovery_eligible = False

    if tier == "reserved":
        warnings.append("scheduler_eligible_false_reserved")
        warnings.append("discovery_eligible_false_reserved")
    elif tier == "controller":
        warnings.append("scheduler_eligible_false_controller")
        warnings.append("discovery_eligible_false_controller")
    else:
        excluded = account_id_excluded_from_scheduler_worker(db, aid)
        if excluded:
            warnings.append("scheduler_excluded_by_allowlist_or_purpose")
        if resolver_code:
            warnings.append("resolver_blocks_telegram_use")
        if status != AccountStatus.ACTIVE.value:
            warnings.append("account_status_not_active")
        if readiness_status != "READY":
            warnings.append("readiness_not_ready")
        if purpose not in _PURPOSE_SCHEDULER_OK and purpose != "both":
            warnings.append("purpose_not_scheduler_compatible")

        if (
            not excluded
            and not resolver_code
            and status == AccountStatus.ACTIVE.value
            and readiness_status == "READY"
            and purpose in _PURPOSE_SCHEDULER_OK
        ):
            scheduler_eligible = True
        elif readiness_status is None:
            warnings.append("readiness_unknown_scheduler_false")

        if purpose not in _PURPOSE_DISCOVERY_OK:
            warnings.append("purpose_not_discovery_compatible")
        if (
            not resolver_code
            and session_exists
            and status == AccountStatus.ACTIVE.value
            and readiness_status == "READY"
            and purpose in _PURPOSE_DISCOVERY_OK
            and tier == "fleet"
            and not excluded
        ):
            discovery_eligible = True
        elif readiness_status is None:
            warnings.append("readiness_unknown_discovery_false")

    if readiness_failure and readiness_failure != resolver_code:
        warnings.append(f"readiness_failure_code:{readiness_failure}")

    return {
        "account_id": aid,
        "label": _account_label(account),
        "tier": tier,
        "schema_version": schema_version,
        "resolver_code": resolver_code,
        "readiness_status": readiness_status,
        "health_status": health_status,
        "scheduler_eligible": scheduler_eligible,
        "discovery_eligible": discovery_eligible,
        "session_path": session_path,
        "session_exists": session_exists,
        "last_resolved_at": last_resolved_at,
        "warnings": warnings,
    }


def compute_operational_states(
    db: Session,
    *,
    account_ids: Optional[list[int]] = None,
) -> list[dict[str, Any]]:
    q = db.query(Account).order_by(Account.id)
    if account_ids:
        q = q.filter(Account.id.in_([int(x) for x in account_ids]))
    rows: list[dict[str, Any]] = []
    for acct in q.all():
        rows.append(compute_account_operational_state(db, acct))
    return rows
