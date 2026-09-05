"""Owner-Messages account eligibility (independent of Story certification)."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from src.ai_agent.account_allowlist import RESERVED_AI_AGENT_ACCOUNT_IDS
from src.core.account_protection import PROTECTED_IDS, PURPOSE_HOLD_IDS
from src.core.models import Account, AccountStatus
from src.clients.readiness_store import (
    STAT_NOT_AUTH,
    STAT_READY,
    fetch_snapshot,
    snapshot_row_valid,
)


@dataclass(frozen=True)
class DmEligibility:
    eligible: bool
    code: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _status_str(account: Account) -> str:
    return str(getattr(account.status, "value", account.status) or "").strip().lower()


def evaluate_dm_account_eligibility(
    db: Session,
    account_id: int,
    *,
    now: Optional[datetime] = None,
) -> DmEligibility:
    """
    Decide whether an account may be used for owner direct messages.

    Story fleet certification is NOT sufficient on its own.
    Reserved AI accounts are excluded from owner Messages.
    """
    now_naive = now or datetime.now(timezone.utc).replace(tzinfo=None)
    aid = int(account_id)

    if aid in PROTECTED_IDS:
        return DmEligibility(False, "PROTECTED", "Account is protected from messaging.")
    if aid in PURPOSE_HOLD_IDS:
        return DmEligibility(False, "RESERVED", "Account is on purpose hold.")
    if aid in RESERVED_AI_AGENT_ACCOUNT_IDS:
        return DmEligibility(
            False, "RESERVED", "Account is reserved for AI Agent; not available for owner Messages."
        )

    account = db.query(Account).filter(Account.id == aid).first()
    if not account:
        return DmEligibility(False, "ACCOUNT_NOT_FOUND", "Account not found.")

    purpose = (account.purpose or "").strip().lower()
    if purpose == "disabled":
        return DmEligibility(False, "DISABLED", "Account purpose is disabled.")

    if not (account.session_string or getattr(account, "session_path", None)):
        return DmEligibility(False, "NEEDS_SESSION", "No usable Telegram session on file.")

    st = _status_str(account)
    if st == AccountStatus.BANNED.value:
        return DmEligibility(False, "BANNED", "Account is banned.")
    if st == AccountStatus.FLOOD_WAIT.value:
        return DmEligibility(False, "FLOOD_WAIT", "Account is in FloodWait cooldown.")
    if st == AccountStatus.AUTH_REQUIRED.value:
        return DmEligibility(False, "AUTH_FAILED", "Account requires re-authentication.")
    if st == AccountStatus.INACTIVE.value:
        return DmEligibility(False, "DISABLED", "Account status is inactive.")
    if st and st != AccountStatus.ACTIVE.value:
        return DmEligibility(False, "SESSION_INVALID", f"Account status is {st}.")

    snap = fetch_snapshot(db, aid)
    if snap and snapshot_row_valid(snap, now_naive):
        if snap.status == STAT_NOT_AUTH:
            return DmEligibility(
                False, "AUTH_FAILED", "Readiness snapshot: Telegram session not authorized."
            )
        if snap.status not in {STAT_READY, "VERIFY"} and snap.status not in {"READY"}:
            # TEMP_CONNECT / ERROR → defer-style deny
            if snap.status in {"TEMP_CONNECT", "ERROR"}:
                return DmEligibility(
                    False,
                    "SESSION_INVALID",
                    f"Readiness snapshot not ready ({snap.status}).",
                )

    return DmEligibility(True, "ELIGIBLE", "Account may send owner direct messages when enabled.")
