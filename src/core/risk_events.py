"""
Record and query account risk events for bulk safety caps.
Used to enforce max_username_changes_per_hour, max_profile_photo_changes_per_hour.
"""
from datetime import datetime, timedelta

from src.utils.helpers import utc_now
from typing import Optional

import structlog
from sqlalchemy import text

from src.core.database import engine

logger = structlog.get_logger(__name__)

EVENT_USERNAME_CHANGED = "username_changed"
EVENT_PROFILE_PHOTO_CHANGED = "profile_photo_changed"
EVENT_MANUAL_REVIEW_CLEARED = "manual_review_cleared"
EVENT_PROFILE_ACTION_OVERRIDE = "profile_action_override"
EVENT_PRECHECK_RUN = "precheck_run"
EVENT_STORY_PUBLISHED = "story_published"


def record_risk_event(account_id: int, event_type: str, details: Optional[str] = None) -> None:
    """Insert event into account_risk_events for audit and hourly caps."""
    try:
        with engine.connect() as conn:
            conn.execute(
                text("""
                    INSERT INTO account_risk_events (account_id, event_type, details)
                    VALUES (:account_id, :event_type, :details)
                """),
                {
                    "account_id": account_id,
                    "event_type": event_type,
                    "details": details or "",
                },
            )
            conn.commit()
    except Exception as e:
        logger.warning("record_risk_event failed", account_id=account_id, event_type=event_type, error=str(e))


def count_events_for_account_since(
    account_id: int, event_type: str, hours: float = 6.0
) -> int:
    """Count events for a specific account in the last N hours (per-account cooldown)."""
    try:
        cutoff = (utc_now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT COUNT(*) FROM account_risk_events "
                    "WHERE account_id = :aid AND event_type = :event_type AND created_at > :cutoff"
                ),
                {"aid": account_id, "event_type": event_type, "cutoff": cutoff},
            ).fetchone()
            return row[0] if row else 0
    except Exception as e:
        logger.warning("count_events_for_account_since failed", account_id=account_id, event_type=event_type, error=str(e))
        return 0


def count_events_last_hour(event_type: str) -> int:
    """Count events of given type in the last hour."""
    try:
        cutoff = (utc_now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT COUNT(*) FROM account_risk_events "
                    "WHERE event_type = :event_type AND created_at > :cutoff"
                ),
                {"event_type": event_type, "cutoff": cutoff},
            ).fetchone()
            return row[0] if row else 0
    except Exception as e:
        logger.warning("count_events_last_hour failed", event_type=event_type, error=str(e))
        return 0


def count_bulk_profile_actions_last_hour() -> int:
    """Count combined username_changed + profile_photo_changed in last hour."""
    return count_events_last_hour(EVENT_USERNAME_CHANGED) + count_events_last_hour(EVENT_PROFILE_PHOTO_CHANGED)
