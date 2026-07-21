"""
P9.13 — Legacy ``account_state`` compatibility shim (web cold-start).

Legacy ``routes.pyc`` imports this module for ``GET /api/accounts/operational-state``.
The P9.1 blueprint in ``operational_state_routes`` is authoritative when enabled.

No Telethon. Does not fake READY. Prefer the P9 route (registered first in ``create_app``).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from config.settings import settings
from src.core.models import Account

BLOCKED_MSG = "blocked_by_p9_source_recovery_no_telethon_connect"
_DISABLED_MSG = "operational_state_api_disabled"

# Stable codes used by runtime_preflight (P9.38 bundle).
BLOCK_SESSION_LOCK_HELD = "session_lock_held"
BLOCK_SESSION_NOT_RESOLVABLE = "session_not_resolvable"
BLOCK_READINESS_NOT_READY = "readiness_not_ready"
BLOCK_NO_READINESS_SNAPSHOT = "no_readiness_snapshot"
BLOCK_PURPOSE_DISABLED = "purpose_disabled"
BLOCK_NOT_AI_AGENT_PERMITTED = "not_ai_agent_permitted"
BLOCK_AI_OR_SCHEDULER_RESERVED = "ai_or_scheduler_reserved"


def operator_label_for_block_code(code: str) -> str:
    """Human label for a stable blocker code (minimal subset for scheduler gate)."""
    head = (code or "").split(":", 1)[0].strip()
    labels = {
        BLOCK_SESSION_LOCK_HELD: "Session lock busy",
        BLOCK_SESSION_NOT_RESOLVABLE: "Telegram session incompatible",
        BLOCK_READINESS_NOT_READY: "Not READY",
        BLOCK_NO_READINESS_SNAPSHOT: "No readiness snapshot",
        BLOCK_PURPOSE_DISABLED: "Purpose disabled",
        BLOCK_NOT_AI_AGENT_PERMITTED: "Not AI-permitted",
        BLOCK_AI_OR_SCHEDULER_RESERVED: "AI / controller reserved",
    }
    return labels.get(head, head.replace("_", " ").title() if head else "Blocked")


@dataclass
class AccountOperationalState:
    """Legacy-shaped row (minimal). Not used when P9 route handles the request."""

    account_id: int
    active: bool = False
    purpose: str = "both"
    ai_reserved: bool = False
    lifecycle_state: str = "OTHER_BLOCKED"
    primary_blocking_reason: Optional[str] = None
    permanent_blockers: List[str] = field(default_factory=list)
    temporary_blockers: List[str] = field(default_factory=list)
    scheduler_eligible: bool = False
    story_eligible: bool = False
    discovery_eligible: bool = False
    ai_agent_eligible: bool = False
    readiness_status: Optional[str] = None
    readiness_expired: bool = True
    story_precheck_status: Optional[str] = None
    blocking_by_purpose: Dict[str, List[str]] = field(default_factory=dict)

    def primary_label(self) -> str:
        return self.primary_blocking_reason or "Unavailable"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FleetStateSummary:
    total_accounts: int = 0
    operational: int = 0
    blocked: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _api_disabled() -> bool:
    return not settings.fleet_operational_state_api_enabled


def _dict_to_legacy(row: Dict[str, Any]) -> AccountOperationalState:
    tier = (row.get("tier") or "").lower()
    lifecycle = "OPERATIONAL"
    if tier == "reserved":
        lifecycle = "AI_RESERVED"
    elif tier == "controller":
        lifecycle = "OTHER_BLOCKED"
    elif row.get("resolver_code"):
        lifecycle = "SESSION_UNREADABLE"
    elif (row.get("readiness_status") or "").upper() not in ("READY",):
        lifecycle = "NOT_READY"

    blockers: List[str] = list(row.get("warnings") or [])
    return AccountOperationalState(
        account_id=int(row["account_id"]),
        active=(row.get("health_status") or "").lower() != "error",
        purpose="both",
        ai_reserved=tier == "reserved",
        lifecycle_state=lifecycle,
        primary_blocking_reason=row.get("resolver_code"),
        permanent_blockers=[b for b in blockers if "immutable" in b or "controller" in b],
        temporary_blockers=blockers,
        scheduler_eligible=bool(row.get("scheduler_eligible")),
        story_eligible=False,
        discovery_eligible=bool(row.get("discovery_eligible")),
        ai_agent_eligible=False,
        readiness_status=row.get("readiness_status"),
        readiness_expired=(row.get("readiness_status") or "").upper() != "READY",
        story_precheck_status=None,
        blocking_by_purpose={"scheduler_message": blockers},
    )


def compute_account_operational_state(
    db: Session,
    account: Account,
    purpose: Optional[str] = None,
) -> AccountOperationalState:
    """
    Legacy API compute hook.

    When the P9 route handles HTTP, this is not called. If invoked while the flag is
    off, returns a conservative blocked state (no READY). If flag on, maps P9.1 dict row.
    """
    if _api_disabled():
        return AccountOperationalState(
            account_id=int(account.id),
            lifecycle_state="OTHER_BLOCKED",
            primary_blocking_reason=_DISABLED_MSG,
            readiness_status="UNKNOWN",
            readiness_expired=True,
        )

    from src.core.account_operational_state import (
        compute_account_operational_state as p9_row,
    )

    return _dict_to_legacy(p9_row(db, account))


def compute_fleet_state_summary(db: Session) -> FleetStateSummary:
    if _api_disabled():
        return FleetStateSummary()
    from src.core.models import Account

    n = db.query(Account).count()
    return FleetStateSummary(total_accounts=n, operational=0, blocked=n)


def lifecycle_state_descriptor(lifecycle_state: str) -> Dict[str, Any]:
    code = (lifecycle_state or "UNKNOWN").upper()
    return {
        "code": code,
        "label": code.replace("_", " ").title(),
        "severity": "info" if code == "OPERATIONAL" else "warning",
    }


def build_operator_account_status(*_args: Any, **_kwargs: Any) -> Dict[str, Any]:
    raise RuntimeError(f"{BLOCKED_MSG}: build_operator_account_status")
