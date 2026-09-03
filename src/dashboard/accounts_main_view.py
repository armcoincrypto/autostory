"""P10.13 / Wave 2 normal Accounts page projection.

Operator-facing, read-only page model. Primary Story status comes from the
canonical fleet readiness matrix via ``operator_account_presentation``.
Page render performs no Telegram network I/O.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from src.ai_agent.account_allowlist import RESERVED_AI_AGENT_ACCOUNT_IDS
from src.core.account_operational_state import CONTROLLER_ACCOUNT_IDS
from src.core.models import Account
from src.core.scheduler_models import JobStatus, ScheduledJob
from src.dashboard.scheduler_mutations import scheduler_mutations_enabled
from src.governance.account_roles import list_pinned_account_ids
from src.governance.governance_resolver import build_execution_eligibility_preview
from src.recovery.p9_83_governance_observability import PROTECTED_IDS, PURPOSE_HOLD_IDS
from src.scheduler.campaign_governance import campaign_execution_enabled
from src.stories.fleet_readiness_matrix import load_latest_matrix, matrix_freshness
from src.stories.operator_account_presentation import (
    build_operator_account_views,
    format_operator_timestamp,
    sort_priority_for_status,
)

import structlog

logger = structlog.get_logger(__name__)

DEFAULT_GOVERNANCE_ROLES_AVAILABLE = [
    "STORY_ALLOWED",
    "DISCOVERY_ALLOWED",
    "SCHEDULER_ALLOWED",
    "LIVE_ALLOWED",
    "QUARANTINED",
]


def empty_eligibility_preview(module: str = "stories") -> dict[str, Any]:
    return {
        "module": module,
        "total_accounts": 0,
        "summary": {
            "eligible": 0,
            "protected": 0,
            "manual_only": 0,
            "quarantined": 0,
            "ai_reserved": 0,
            "operational_blocked": 0,
            "missing_capability_role": 0,
        },
        "details": [],
        "visibility_only": True,
        "runtime_execution_unchanged": True,
        "preview_unavailable": True,
    }


def build_eligibility_preview_safe(db: Session, *, module: str) -> dict[str, Any]:
    try:
        return build_execution_eligibility_preview(db, module=module)
    except Exception as exc:
        logger.warning(
            "accounts_eligibility_preview_failed",
            module=module,
            error=str(exc),
        )
        return empty_eligibility_preview(module)


def _status_value(account: Account) -> str:
    status = getattr(account, "status", None)
    return status.value if hasattr(status, "value") else str(status or "")


def _label(account: Account) -> str:
    username = (account.username or "").strip()
    if username:
        return username if username.startswith("@") else f"@{username}"
    phone = (account.phone_number or "").strip()
    if phone:
        return phone
    first = (getattr(account, "first_name", None) or "").strip()
    last = (getattr(account, "last_name", None) or "").strip()
    name = " ".join(x for x in (first, last) if x)
    if name:
        return name
    return f"account_{int(account.id)}"


def _sanitize_proxy(proxy_config: Any) -> dict[str, Any]:
    """Configured/not configured only — never credentials."""
    if not proxy_config:
        return {"configured": False, "label": "Not configured"}
    if isinstance(proxy_config, str):
        text = proxy_config.strip()
        if not text:
            return {"configured": False, "label": "Not configured"}
        # Redact anything that looks like user:pass@
        if "@" in text:
            host = text.rsplit("@", 1)[-1]
            return {"configured": True, "label": f"Configured ({host})"}
        return {"configured": True, "label": "Configured"}
    if isinstance(proxy_config, dict):
        host = (
            proxy_config.get("host")
            or proxy_config.get("server")
            or proxy_config.get("hostname")
            or proxy_config.get("addr")
        )
        scheme = proxy_config.get("scheme") or proxy_config.get("type") or proxy_config.get("protocol") or "proxy"
        port = proxy_config.get("port")
        if host and port:
            return {"configured": True, "label": f"{scheme}://{host}:{port}"}
        if host:
            return {"configured": True, "label": f"{scheme}://{host}"}
        return {"configured": True, "label": "Configured"}
    return {"configured": True, "label": "Configured"}


def _batch_active_jobs(db: Session) -> dict[int, dict[str, Any]]:
    """One query for all PENDING/RUNNING scheduler jobs → owner-facing current job."""
    out: dict[int, dict[str, Any]] = {}
    try:
        rows = (
            db.query(ScheduledJob.account_id, ScheduledJob.type, ScheduledJob.status, ScheduledJob.id)
            .filter(
                ScheduledJob.status.in_(
                    [JobStatus.PENDING.value, JobStatus.RUNNING.value, "PENDING", "RUNNING"]
                )
            )
            .order_by(ScheduledJob.id.asc())
            .all()
        )
    except Exception as exc:
        logger.warning("accounts_active_jobs_batch_failed", error=str(exc))
        return out
    for account_id, job_type, status, job_id in rows:
        aid = int(account_id)
        if aid in out:
            continue
        jtype = str(job_type or "").upper()
        st = str(status or "").upper()
        if jtype in {"PROMO", "INFO"}:
            label = "Scheduled task"
        elif jtype in {"DM", "MESSAGE", "SEND_MESSAGE"}:
            label = "Message task"
        else:
            label = "Scheduled task"
        if st == "RUNNING":
            label = f"{label} (running)"
        out[aid] = {
            "label": label,
            "job_type": jtype,
            "job_status": st,
            "job_id": int(job_id) if job_id is not None else None,
        }
    return out


def _last_story_display(account: Account, matrix_last: Any) -> dict[str, Any]:
    raw = getattr(account, "last_story_success_at", None) or matrix_last
    if not raw:
        return {"label": "Never", "iso": None, "has_story": False}
    iso = raw.isoformat() if hasattr(raw, "isoformat") else str(raw)
    return {
        "label": format_operator_timestamp(iso) or "Never",
        "iso": iso,
        "has_story": True,
    }


def _flood_display(account: Account, canonical: dict[str, Any]) -> str:
    until = getattr(account, "flood_wait_until", None)
    if until is not None:
        try:
            now = datetime.now(timezone.utc)
            dt = until if until.tzinfo else until.replace(tzinfo=timezone.utc)
            if dt > now:
                return f"Active until {format_operator_timestamp(dt.isoformat())}"
        except Exception:
            return "Active"
    secs = canonical.get("flood_wait_seconds")
    if secs:
        return f"{secs}s reported"
    return "None"


def _role_chip(display_status: str, role_label: str) -> str | None:
    if display_status in {"PROTECTED", "RESERVED"}:
        return role_label
    return None


def _can_open_stories(display_status: str) -> bool:
    return display_status in {"READY", "CERTIFIED"}


def build_accounts_main_context(db: Session) -> dict[str, Any]:
    pinned_ids = set(list_pinned_account_ids(db))
    matrix = load_latest_matrix()
    freshness = matrix_freshness(matrix)
    operator_views = build_operator_account_views(matrix, freshness=freshness)
    mapped_by_id = operator_views["accounts_by_id"]
    active_jobs = _batch_active_jobs(db)

    accounts = db.query(Account).order_by(Account.id.asc()).all()
    rows: list[dict[str, Any]] = []
    for account in accounts:
        aid = int(account.id)
        presentation = mapped_by_id.get(aid)
        if presentation is None:
            presentation = {
                "account_id": aid,
                "display_status": "CHECK_REQUIRED",
                "status_label": "Check required",
                "status_detail": "Health check required",
                "authorization_label": "Needs check",
                "role_label": "Unknown",
                "required_action": "Refresh fleet authorization",
                "severity": "warning",
                "diagnostic_reason": "missing_from_canonical_matrix",
                "row_action": "view_details",
                "tooltip": "Certification evidence missing — not necessarily a broken account.",
                "last_checked": None,
                "last_story_at_matrix": None,
                "filter_group": "needs_attention",
                "sort_priority": 2,
                "canonical": {},
            }

        display_status = presentation["display_status"]
        canonical = presentation.get("canonical") or {}
        job = active_jobs.get(aid)
        last_story = _last_story_display(account, presentation.get("last_story_at_matrix") or canonical.get("last_story_at"))
        proxy = _sanitize_proxy(getattr(account, "proxy_config", None))
        stories_today = int(getattr(account, "stories_today", 0) or 0)
        flood = _flood_display(account, canonical)

        label = _label(account)
        username = (account.username or "").strip()
        rows.append(
            {
                "id": aid,
                "label": label,
                "phone": account.phone_number or "",
                "username": username,
                "telegram_user_id": getattr(account, "user_id", None),
                "status": _status_value(account) or "unknown",
                "purpose": (account.purpose or "both").strip().lower() or "both",
                "pinned": aid in pinned_ids,
                # Primary operator presentation (canonical mapper).
                "display_status": display_status,
                "status_label": presentation["status_label"],
                "status_detail": presentation["status_detail"],
                "authorization_label": presentation["authorization_label"],
                "role_label": presentation["role_label"],
                "role_chip": _role_chip(display_status, presentation["role_label"]),
                "required_action": presentation["required_action"],
                "severity": presentation["severity"],
                "diagnostic_reason": presentation["diagnostic_reason"],
                "row_action": presentation["row_action"],
                "tooltip": presentation.get("tooltip"),
                "last_checked": format_operator_timestamp(presentation.get("last_checked")) or presentation.get("last_checked"),
                "last_checked_raw": presentation.get("last_checked"),
                "last_story_label": last_story["label"],
                "last_story_iso": last_story["iso"],
                "current_job_label": job["label"] if job else "Idle",
                "current_job": job,
                "stories_today": stories_today,
                "proxy": proxy,
                "flood_wait": flood,
                "filter_group": presentation.get("filter_group") or "all",
                "sort_priority": int(presentation.get("sort_priority") or sort_priority_for_status(display_status)),
                "canonical": canonical,
                "can_import_session": presentation.get("row_action") == "import_session" or display_status in {"NEEDS_SESSION", "BLOCKED"},
                "can_open_stories": _can_open_stories(display_status),
                "is_unavailable": display_status in {"DISABLED", "PROTECTED", "RESERVED"},
                "modules": {"campaigns": {"label": "—"}},
                "safety_note": (
                    "Protected"
                    if aid in PROTECTED_IDS or aid in CONTROLLER_ACCOUNT_IDS
                    else (
                        "AI reserved"
                        if aid in RESERVED_AI_AGENT_ACCOUNT_IDS
                        else ("Held" if aid in PURPOSE_HOLD_IDS else None)
                    )
                ),
            }
        )

    rows.sort(key=lambda r: (r["sort_priority"], not r["pinned"], r["id"]))
    eligibility = build_eligibility_preview_safe(db, module="stories")
    scheduler_preview = build_eligibility_preview_safe(db, module="scheduler")
    discovery_preview = build_eligibility_preview_safe(db, module="discovery")
    summary = operator_views["summary"]

    return {
        "accounts": rows,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "total_accounts": summary["total_accounts"] or len(rows),
        "operator_summary": summary,
        "freshness_compact": summary["freshness_compact"],
        "default_filter": summary["default_filter"],
        "scheduler_locked": not scheduler_mutations_enabled(),
        "campaigns_locked": not campaign_execution_enabled(),
        "automation_locked": True,
        "pinned_account_ids": sorted(pinned_ids),
        "eligibility_preview": eligibility,
        "eligibility_scheduler": scheduler_preview,
        "eligibility_discovery": discovery_preview,
        "governance_roles_available": list(DEFAULT_GOVERNANCE_ROLES_AVAILABLE),
        "canonical_source": operator_views.get("canonical_source"),
        "matrix_fresh": bool(freshness.get("fresh")),
        "page_perf": {
            "external_telegram_calls_on_page_load": 0,
            "active_jobs_batched": True,
            "accounts_loaded": len(rows),
        },
    }
