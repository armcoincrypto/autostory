"""Bounded fleet production-certification wave selection helpers.

Selection only — does not publish. Used by ops orchestration and unit tests.
"""
from __future__ import annotations

from typing import Any


EXCLUDED_CLASSIFICATIONS = frozenset(
    {
        "CERTIFIED_PUBLISH",
        "ACCOUNT_DISABLED",
        "INTENTIONALLY_EXCLUDED",
        "PROTECTED",
        "RESERVED",
        "AUTH_FAILED",
        "AUTH_STALE",
        "CONFIG_INCOMPLETE",
        "UNKNOWN",
        "BANNED",
        "FLOOD_WAIT",
        "IDENTITY_MISMATCH",
        "SESSION_CORRUPT",
        "SESSION_CONFLICT",
        "COUNTER_INVALID",
        "STORY_CAPABILITY_FAILED",
    }
)

READY_CLASSIFICATION = "READY_FOR_SEPARATE_CONTROLLED_CANARY"


def select_certification_wave(
    accounts: list[dict[str, Any]],
    *,
    wave_size: int = 5,
    exclude_ids: set[int] | None = None,
    max_wave_size: int = 10,
) -> list[dict[str, Any]]:
    """Rank READY accounts and return up to ``wave_size`` candidates.

    Ranking: safety score ascending, then oldest account_id.
    Pass ``exclude_ids`` from durable certification evidence — do not hardcode.
    Does not unlock or publish.
    """
    if wave_size < 1:
        return []
    if wave_size > max_wave_size:
        raise ValueError(f"wave_size {wave_size} exceeds max_wave_size {max_wave_size}")
    exclude = set(exclude_ids or ())

    ranked: list[dict[str, Any]] = []
    for raw in accounts:
        aid = int(raw.get("account_id") or raw.get("id") or 0)
        if not aid or aid in exclude:
            continue
        classification = str(raw.get("classification") or "")
        if classification != READY_CLASSIFICATION:
            continue
        if classification in EXCLUDED_CLASSIFICATIONS:
            continue

        stories_today = int(raw.get("stories_today_effective") or raw.get("stories_today") or 0)
        daily_limit = int(raw.get("daily_limit") or 1)
        score = 0
        reasons: list[str] = []

        if not raw.get("auth_valid"):
            score += 100
            reasons.append("auth_invalid")
        if raw.get("identity_matches") is False:
            score += 100
            reasons.append("identity_mismatch")
        if not raw.get("story_api_available"):
            score += 50
            reasons.append("story_api_unavailable")
        sps = str(raw.get("story_probe_status") or "")
        if sps and sps not in {"allowed", "ok", "success", "can_send", "not_probed", "not_run"}:
            score += 20
            reasons.append(f"probe:{sps}")
        if stories_today > 0:
            score += 80
            reasons.append("no_capacity")
        if stories_today >= daily_limit:
            score += 80
            reasons.append("at_daily_limit")
        if raw.get("safe_error_summary"):
            score += 5
            reasons.append("operator_warning")

        if score >= 50:
            continue

        ranked.append(
            {
                "account_id": aid,
                "telegram_user_id": raw.get("telegram_user_id")
                or raw.get("expected_telegram_user_id"),
                "identity": raw.get("display_name")
                or raw.get("telegram_username")
                or raw.get("expected_username"),
                "last_probe_time": raw.get("last_auth_at"),
                "stories_today": stories_today,
                "daily_limit": daily_limit,
                "session_status": "readable" if raw.get("session_readable") else "unreadable",
                "classification": classification,
                "score": score,
                "selection_reason": reasons
                or ["ready_classification", "capacity_available", "oldest_after_safety"],
            }
        )

    ranked.sort(key=lambda r: (int(r["score"]), int(r["account_id"])))
    return ranked[:wave_size]


def allowlist_equals_wave(allowlist: str | list[int] | set[int], wave_ids: list[int]) -> bool:
    if isinstance(allowlist, str):
        parts = [p.strip() for p in allowlist.split(",") if p.strip()]
        ids = {int(p) for p in parts}
    else:
        ids = {int(x) for x in allowlist}
    return ids == {int(x) for x in wave_ids}
