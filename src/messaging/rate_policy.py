"""Conservative owner-DM pacing / caps (send remains disabled until flag on)."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from src.messaging.models import STATUS_SENT, OwnerDmIntent


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return int(default)
    try:
        return max(0, int(str(raw).strip(), 10))
    except ValueError:
        return int(default)


def load_owner_dm_rate_config() -> dict[str, int]:
    return {
        "min_account_interval_sec": _env_int("DM_MIN_ACCOUNT_INTERVAL_SEC", 60),
        "hourly_cap": _env_int("DM_HOURLY_CAP", 20),
        "daily_cap": _env_int("DM_DAILY_CAP", 50),
    }


def evaluate_owner_dm_rate_policy(
    db: Session,
    account_id: int,
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Return allowed + reason without mutating."""
    cfg = load_owner_dm_rate_config()
    now_naive = now or datetime.now(timezone.utc).replace(tzinfo=None)
    aid = int(account_id)

    last = (
        db.query(OwnerDmIntent)
        .filter(OwnerDmIntent.account_id == aid, OwnerDmIntent.status == STATUS_SENT)
        .order_by(OwnerDmIntent.sent_at.desc())
        .first()
    )
    if last and last.sent_at is not None:
        elapsed = (now_naive - last.sent_at).total_seconds()
        need = int(cfg["min_account_interval_sec"])
        if elapsed < need:
            return {
                "allowed": False,
                "code": "RATE_LIMITED",
                "reason": f"Per-account pacing: wait {int(need - elapsed)}s.",
                "retry_after": int(need - elapsed),
                "config": cfg,
            }

    hour_ago = now_naive - timedelta(hours=1)
    day_ago = now_naive - timedelta(days=1)
    hourly = (
        db.query(OwnerDmIntent)
        .filter(
            OwnerDmIntent.account_id == aid,
            OwnerDmIntent.status == STATUS_SENT,
            OwnerDmIntent.sent_at >= hour_ago,
        )
        .count()
    )
    if hourly >= int(cfg["hourly_cap"]):
        return {
            "allowed": False,
            "code": "RATE_LIMITED",
            "reason": f"Hourly owner-DM cap reached ({cfg['hourly_cap']}).",
            "retry_after": 3600,
            "config": cfg,
        }
    daily = (
        db.query(OwnerDmIntent)
        .filter(
            OwnerDmIntent.account_id == aid,
            OwnerDmIntent.status == STATUS_SENT,
            OwnerDmIntent.sent_at >= day_ago,
        )
        .count()
    )
    if daily >= int(cfg["daily_cap"]):
        return {
            "allowed": False,
            "code": "RATE_LIMITED",
            "reason": f"Daily owner-DM cap reached ({cfg['daily_cap']}).",
            "retry_after": 86400,
            "config": cfg,
        }
    return {"allowed": True, "code": "OK", "reason": "Within owner-DM rate limits.", "config": cfg}
