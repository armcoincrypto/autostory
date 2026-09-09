"""Owner-facing account health from the canonical fleet matrix (Wave E).

Accounts page already uses ``operator_account_presentation``. This module is the
shared helper for API rows so consumers do not treat stale ORM ``health_status``
as owner truth.

ORM ``health_status`` remains an internal/historical field (healthcheck writers).
It must not drive owner Health badges or filters.
"""
from __future__ import annotations

from typing import Any, Optional

from src.stories.operator_account_presentation import (
    OWNER_HEALTH_LABELS,
    build_operator_account_views,
)


def build_owner_health_index(
    matrix: Any = None,
    *,
    freshness: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Return presentation index + freshness for owner_health enrichment."""
    views = build_operator_account_views(matrix, freshness=freshness)
    return {
        "by_id": views.get("accounts_by_id") or {},
        "summary": views.get("summary") or {},
        "freshness": views.get("freshness") or {},
        "canonical_source": views.get("canonical_source"),
        "matrix_fresh": bool((views.get("freshness") or {}).get("fresh")),
    }


def owner_health_payload_for_presentation(presentation: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Map a presentation row into additive API owner_health fields."""
    if not presentation:
        return {
            "owner_health": "CHECK_REQUIRED",
            "owner_health_label": OWNER_HEALTH_LABELS["CHECK_REQUIRED"],
            "owner_authorization_label": "Needs check",
            "owner_filter_group": "needs_attention",
            "owner_health_source": "canonical_fleet_matrix_missing_row",
            "owner_health_detail": "Health check required",
        }
    display = str(presentation.get("display_status") or "CHECK_REQUIRED")
    return {
        "owner_health": display,
        "owner_health_label": presentation.get("status_label")
        or OWNER_HEALTH_LABELS.get(display, display),
        "owner_authorization_label": presentation.get("authorization_label") or "Needs check",
        "owner_filter_group": presentation.get("filter_group") or "all",
        "owner_health_source": "canonical_fleet_matrix",
        "owner_health_detail": presentation.get("status_detail"),
    }


def attach_owner_health(
    row: dict[str, Any],
    *,
    index: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Attach additive owner_health fields; preserve raw ORM health_status."""
    idx = index or build_owner_health_index()
    by_id = idx.get("by_id") or {}
    aid = row.get("id") or row.get("account_id")
    try:
        aid_i = int(aid) if aid is not None else None
    except (TypeError, ValueError):
        aid_i = None
    presentation = by_id.get(aid_i) if aid_i is not None else None
    payload = owner_health_payload_for_presentation(presentation)
    if not idx.get("matrix_fresh") and payload["owner_health"] in {"CERTIFIED", "READY"}:
        # Defense in depth — mapper already fail-closes, but never emit fresh labels on stale index.
        payload = owner_health_payload_for_presentation(
            {
                "display_status": "CHECK_REQUIRED",
                "status_label": OWNER_HEALTH_LABELS["CHECK_REQUIRED"],
                "authorization_label": "Needs check",
                "filter_group": "needs_attention",
                "status_detail": "Health check required",
            }
        )
        payload["owner_health_source"] = "canonical_fleet_matrix_stale"
    row.update(payload)
    # Explicit legacy alias — consumers must prefer owner_health.
    if "health_status" in row and "raw_health_status" not in row:
        row["raw_health_status"] = row.get("health_status")
    return row


def owner_summary_from_index(index: dict[str, Any]) -> dict[str, Any]:
    """Compact owner summary suitable for API summary payloads."""
    s = index.get("summary") or {}
    return {
        "owner_total": int(s.get("total_accounts") or 0),
        "owner_certified": int(s.get("certified") or 0),
        "owner_ready": int(s.get("ready") or 0),
        "owner_authorized": int(s.get("authorized") or 0),
        "owner_needs_attention": int(s.get("needs_attention") or 0),
        "owner_blocked": int(s.get("blocked") or 0),
        "owner_check_required": int(s.get("check_required") or 0),
        "owner_disabled": int(s.get("disabled") or 0),
        "owner_unavailable": int(s.get("unavailable") or 0),
        "owner_matrix_fresh": bool(index.get("matrix_fresh")),
        "owner_health_source": "canonical_fleet_matrix",
    }
