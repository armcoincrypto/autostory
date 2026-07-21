"""
P9.18 — Read-only fleet account ID discovery for accounts v2 dashboard.

Unions DB ``accounts``, canonical session files, and v1 readiness snapshots.
No Telethon, no writes.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from src.core.models import Account
from src.core.scheduler_models import AccountReadinessSnapshot
from src.core.session_paths import get_existing_canonical_account_ids

VALID_TIER_FILTERS = frozenset({"fleet", "reserved", "controller"})
VALID_STATUS_FILTERS = frozenset(
    {
        "LEGACY_SCHEMA",
        "READY",
        "ERROR",
        "RESERVED",
        "CONTROLLER",
        "UNKNOWN",
        "NOT_FOUND",
        "TABLE_MISSING",
    }
)
MAX_PAGE_LIMIT = 500


def discover_fleet_account_ids(db: Session) -> list[int]:
    """
    Discover account IDs for full-fleet listing.

    Prefer ``accounts`` table; union session files and v1 readiness rows; dedupe; sort.
    """
    ids: set[int] = set()
    for row in db.query(Account.id).all():
        ids.add(int(row[0]))
    ids.update(get_existing_canonical_account_ids())
    for row in db.query(AccountReadinessSnapshot.account_id).distinct().all():
        if row[0] is not None:
            ids.add(int(row[0]))
    return sorted(ids)


def parse_ids_param(raw: str) -> list[int]:
    """Parse comma-separated account ids from query string."""
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        out.append(int(part))
    return out


def normalize_tier_filter(raw: Optional[str]) -> Optional[str]:
    if not raw or not str(raw).strip():
        return None
    t = str(raw).strip().lower()
    if t not in VALID_TIER_FILTERS:
        raise ValueError(f"invalid tier filter: {raw}")
    return t


def normalize_status_filter(raw: Optional[str]) -> Optional[str]:
    if not raw or not str(raw).strip():
        return None
    s = str(raw).strip().upper()
    if s not in VALID_STATUS_FILTERS:
        raise ValueError(f"invalid status filter: {raw}")
    return s


def normalize_pagination(
    offset: Optional[int],
    limit: Optional[int],
) -> tuple[int, Optional[int]]:
    off = max(0, int(offset or 0))
    if limit is None:
        return off, None
    lim = min(max(1, int(limit)), MAX_PAGE_LIMIT)
    return off, lim


def filter_account_rows(
    rows: list[dict],
    *,
    tier: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict]:
    out = rows
    if tier:
        out = [r for r in out if r.get("tier_raw") == tier]
    if status:
        out = [
            r
            for r in out
            if (r.get("display_status") or "").upper() == status
            or (str(r.get("v2_status") or "")).upper() == status
            or (str(r.get("v1_status") or "")).upper() == status
        ]
    return out


def paginate_rows(
    rows: list[dict],
    *,
    offset: int = 0,
    limit: Optional[int] = None,
) -> list[dict]:
    if limit is None:
        return rows[offset:]
    return rows[offset : offset + limit]
