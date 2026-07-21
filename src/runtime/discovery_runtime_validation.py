"""P10.11 Discovery runtime validation (safe/read-only planning)."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from src.core.account_operational_state import compute_account_operational_state
from src.core.models import Account, DiscoveredUser


def validate_discovery_runtime(db: Session, *, account_ids: list[int] | None = None) -> dict[str, Any]:
    q = db.query(Account).order_by(Account.id.asc())
    if account_ids:
        q = q.filter(Account.id.in_([int(x) for x in account_ids]))

    candidates = []
    for acc in q.all():
        op = compute_account_operational_state(db, acc)
        blockers = []
        if not op.get("discovery_eligible"):
            blockers.append("discovery_not_eligible")
        if op.get("resolver_code"):
            blockers.append(f"resolver_blocked:{op.get('resolver_code')}")
        candidates.append(
            {
                "account_id": int(acc.id),
                "usable": not blockers,
                "blockers": blockers,
                "discovery_eligible": op.get("discovery_eligible"),
            }
        )

    week_ago = datetime.utcnow() - timedelta(days=7)
    return {
        "track": "C",
        "outcome": "DISCOVERY_RUNTIME_READY",
        "candidate_accounts": candidates,
        "discovered_users_total": db.query(DiscoveredUser).count(),
        "recent_discovered_users_7d": db.query(DiscoveredUser)
        .filter(DiscoveredUser.discovered_at >= week_ago)
        .count(),
        "blocked_users": db.query(DiscoveredUser).filter(DiscoveredUser.is_blocked == True).count(),
        "safety_note": "P10.11 discovery validation does not scrape Telegram or create large target pools.",
    }
