"""
P9.8+ — Accounts dashboard v2 shell (offline preview + optional live route).

Live route: ``GET /accounts-v2`` via ``accounts_v2_routes`` when
``ACCOUNTS_V2_DASHBOARD_ENABLED`` is true (default false).

Offline: ``scripts/ops/render_accounts_v2_preview.py`` → static HTML.

Read-only: manager_v2, operational state, readiness v2 observer. No Telethon, no DB writes.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.orm import Session

from src.clients.manager_v2 import get_account_metadata
from src.core.models import Account
from src.dashboard.accounts_v2_fleet import (
    discover_fleet_account_ids,
    filter_account_rows,
    paginate_rows,
)
from src.recovery.fleet_recovery_summary import build_fleet_recovery_summary
from src.recovery.p10_8_global_active_job_hygiene import build_account_usability_fields
from src.dashboard.accounts_v2_module_usability import build_module_usability_block
from src.governance.governance_resolver import resolve_account_governance
from src.readiness.readiness_v2_observer import (
    build_v2_snapshot_row,
    compare_readiness_v1_v2,
    explain_v1_v2_mismatch,
)

# Mirrors scripts/ops/pre_restart_guard.sh (read-only check for preview banner).
_RESTART_GUARD_REQUIRED: tuple[str, ...] = (
    "src/clients/manager.py",
    "src/clients/readiness_worker.py",
    "src/clients/readiness_store.py",
    "src/scheduler/executor.py",
    "src/scheduler/runtime_preflight.py",
    "src/core/session_lock.py",
    "src/dashboard/routes.py",
    "src/dashboard/templates/accounts.html",
    "scripts/ops/regenerate_telethon_session.py",
    "src/bot/kathleen_account_listener.py",
)

DEFAULT_PILOT_ACCOUNT_IDS: tuple[int, ...] = (106, 110, 206)

_TIER_ORDER = ("reserved", "controller", "fleet")
_STATUS_CSS = {
    "LEGACY_SCHEMA": "status-legacy",
    "RESERVED": "status-reserved",
    "CONTROLLER": "status-controller",
    "ERROR": "status-error",
    "UNKNOWN": "status-unknown",
    "NOT_FOUND": "status-unknown",
    "READY": "status-ready",
    "AUTH_OK_NOT_ENABLED": "status-auth-ok",
    "TABLE_MISSING": "status-error",
}


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def template_dir() -> Path:
    return Path(__file__).resolve().parent / "templates"


def static_css_path() -> Path:
    return Path(__file__).resolve().parent / "static" / "css" / "accounts_v2.css"


def check_restart_guard(root: Optional[Path] = None) -> dict[str, Any]:
    """Read-only mirror of pre_restart_guard missing-file check."""
    base = (root or project_root()).resolve()
    missing = [rel for rel in _RESTART_GUARD_REQUIRED if not (base / rel).is_file()]
    return {
        "active": bool(missing),
        "missing_files": missing,
        "message": (
            "BLOCKED: source/runtime drift unresolved. Do not restart."
            if missing
            else "Guard clear (operator approval still required for restart)."
        ),
    }


def _tier_label(tier: Optional[str]) -> str:
    t = (tier or "unknown").strip().lower()
    if t == "reserved":
        return "RESERVED"
    if t == "controller":
        return "CONTROLLER"
    if t == "fleet":
        return "FLEET"
    return "UNKNOWN"


def _status_css(status: Optional[str]) -> str:
    key = (status or "UNKNOWN").strip().upper()
    return _STATUS_CSS.get(key, "status-unknown")


def build_account_row(
    db: Session,
    account_id: int,
    *,
    compare_row: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Merge manager_v2 + v2 snapshot + v1/v2 compare for one account card."""
    aid = int(account_id)
    meta = get_account_metadata(db, aid, include_lock=True)
    v2 = build_v2_snapshot_row(db, aid)
    if compare_row is None:
        cmp_rows = compare_readiness_v1_v2(db, [aid])
        compare_row = cmp_rows[0] if cmp_rows else {}

    tier = meta.get("tier") or v2.get("tier")
    display_status = (v2.get("v2_status") or "UNKNOWN").upper()
    v1_status = compare_row.get("v1_status") or meta.get("readiness_snapshot_status")

    warnings = list(dict.fromkeys(
        (meta.get("warnings") or []) + (v2.get("warnings") or [])
    ))

    lock = meta.get("session_lock") or {}
    usability = build_account_usability_fields(db, aid)
    account = db.get(Account, aid) if hasattr(db, "get") else db.query(Account).filter(Account.id == aid).first()
    governance = resolve_account_governance(db, account) if account else {"badges": [], "blocked_reasons": []}
    return {
        "account_id": aid,
        "label": meta.get("label") or f"account_{aid}",
        "tier": _tier_label(tier),
        "tier_raw": (tier or "unknown").lower(),
        "display_status": display_status,
        "status_css": _status_css(display_status),
        "v1_status": v1_status,
        "v2_status": v2.get("v2_status"),
        "v2_reason": v2.get("v2_reason"),
        "failure_code": (
            v2.get("failure_code")
            if v2.get("v2_status") not in (None, "NOT_FOUND", "TABLE_MISSING")
            else meta.get("readiness_failure_code")
        ),
        "v1_checked_at": compare_row.get("v1_checked_at"),
        "v2_checked_at": v2.get("checked_at"),
        "mismatch_reason": compare_row.get("mismatch_reason")
        or explain_v1_v2_mismatch(
            v1_status=str(v1_status),
            v2_status=str(v2.get("v2_status")),
            v1_failure_code=v2.get("failure_code"),
            tier=tier,
        ),
        "schema_version": v2.get("schema_version") or meta.get("schema_version"),
        "resolver_code": meta.get("resolver_code"),
        "scheduler_eligible": meta.get("scheduler_eligible"),
        "discovery_eligible": meta.get("discovery_eligible"),
        "session_exists": meta.get("session_exists"),
        "session_path": meta.get("session_path"),
        "session_lock_held": lock.get("held"),
        "warnings": warnings,
        "found": meta.get("found", True),
        "session_label": usability.get("session_label"),
        "auth_label": usability.get("auth_label"),
        "resolver_label": usability.get("resolver_label"),
        "technical_health_label": usability.get("technical_health_label"),
        "scheduler_queue_label": usability.get("scheduler_queue_label"),
        "purpose_label": usability.get("purpose_label"),
        "campaign_eligibility_label": usability.get("campaign_eligibility_label"),
        "active_job_count": usability.get("active_job_count"),
        "purpose_raw": usability.get("purpose_raw"),
        "module_usability": build_module_usability_block(db, aid, meta=meta),
        "governance_badges": governance.get("badges") or [],
        "governance_blocked_reasons": governance.get("blocked_reasons") or [],
        "requires_manual_override": bool(governance.get("requires_manual_override")),
    }


def _sort_account_rows(accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    accounts.sort(
        key=lambda a: (
            _TIER_ORDER.index(a["tier_raw"])
            if a["tier_raw"] in _TIER_ORDER
            else 99,
            a["account_id"],
        )
    )
    return accounts


def build_accounts_for_ids(db: Session, account_ids: list[int]) -> list[dict[str, Any]]:
    """Build sorted account card rows for explicit ids (read-only)."""
    ids = [int(x) for x in account_ids]
    compares = {r["account_id"]: r for r in compare_readiness_v1_v2(db, ids)}
    rows = [
        build_account_row(db, aid, compare_row=compares.get(aid))
        for aid in ids
    ]
    return _sort_account_rows(rows)


def build_accounts_v2_listing(
    db: Session,
    *,
    account_ids: Optional[list[int]] = None,
    tier_filter: Optional[str] = None,
    status_filter: Optional[str] = None,
    offset: int = 0,
    limit: Optional[int] = None,
    root: Optional[Path] = None,
    css_href: Optional[str] = None,
    live_route: bool = False,
) -> dict[str, Any]:
    """
    Full listing context: discover fleet (unless ids provided), filter, paginate.

    ``account_ids=None`` triggers :func:`discover_fleet_account_ids`.
    """
    if account_ids is None:
        discovered_ids = discover_fleet_account_ids(db)
        id_source = "discovered"
    else:
        discovered_ids = [int(x) for x in account_ids]
        id_source = "query_ids"

    all_rows = build_accounts_for_ids(db, discovered_ids)
    total_discovered = len(discovered_ids)
    filtered_rows = filter_account_rows(
        all_rows,
        tier=tier_filter,
        status=status_filter,
    )
    total_after_filters = len(filtered_rows)
    page_rows = paginate_rows(filtered_rows, offset=offset, limit=limit)
    shown_ids = [r["account_id"] for r in page_rows]

    recovery_summary = build_fleet_recovery_summary(
        db,
        discovered_ids,
        include_restart_guard_warning=False,
    )

    guard = check_restart_guard(root)
    if guard.get("active") and guard.get("message"):
        recovery_summary["warnings"] = list(recovery_summary.get("warnings") or []) + [
            guard["message"],
        ]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return {
        "accounts": page_rows,
        "recovery_summary": recovery_summary,
        "account_ids": shown_ids,
        "total_discovered": total_discovered,
        "total_after_filters": total_after_filters,
        "total_shown": len(page_rows),
        "id_source": id_source,
        "offset": offset,
        "limit": limit,
        "tier_filter": tier_filter,
        "status_filter": status_filter,
        "generated_at": now,
        "restart_guard_active": guard["active"],
        "restart_guard_message": guard["message"],
        "restart_guard_missing": guard["missing_files"],
        "v2_authoritative": False,
        "preview_mode": not live_route,
        "live_route": live_route,
        "css_href": css_href or "../../src/dashboard/static/css/accounts_v2.css",
    }


def build_preview_context(
    db: Session,
    account_ids: list[int],
    *,
    root: Optional[Path] = None,
    css_href: Optional[str] = None,
    live_route: bool = False,
) -> dict[str, Any]:
    """Full Jinja context for offline accounts v2 preview (explicit ids)."""
    return build_accounts_v2_listing(
        db,
        account_ids=account_ids,
        root=root,
        css_href=css_href,
        live_route=live_route,
    )


def render_accounts_v2_html(
    context: dict[str, Any],
    *,
    templates: Optional[Path] = None,
) -> str:
    """Render standalone HTML via Jinja2 (no Flask app)."""
    tdir = templates or template_dir()
    env = Environment(
        loader=FileSystemLoader(str(tdir)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = env.get_template("accounts_v2.html")
    return template.render(**context)
