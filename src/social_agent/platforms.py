"""Provider-independent platform catalog for Content Studio previews and variants.

Preview rendering for Meta destinations reuses canonical Meta renderers.
Non-Meta platforms expose UI/preview shapes only — never provider HTTP.
"""
from __future__ import annotations

from typing import Any

# Char / caption limits used by variant validation and studio meters.
PLATFORM_LIMITS: dict[str, int] = {
    "facebook": 63206,
    "instagram": 2200,
    "telegram": 4096,
    "x": 280,
    "linkedin": 3000,
    "tiktok": 2200,
    "youtube_community": 5000,
    "discord": 2000,
}

# Content Studio preview tabs (canonical order).
STUDIO_PLATFORMS: tuple[str, ...] = (
    "facebook",
    "instagram",
    "telegram",
    "x",
    "linkedin",
    "tiktok",
    "youtube_community",
    "discord",
)

# Default platforms when generating multi-destination variants.
DEFAULT_VARIANT_PLATFORMS: tuple[str, ...] = STUDIO_PLATFORMS

# Map studio platform → publishing dry-run destination (Meta only today).
STUDIO_TO_META_DESTINATION: dict[str, str] = {
    "facebook": "facebook_page",
    "instagram": "instagram_feed",
}

PROVIDER_LABELS: dict[str, str] = {
    "meta": "Meta (Facebook / Instagram)",
    "telegram": "Telegram",
    "x": "X (Twitter)",
    "linkedin": "LinkedIn",
    "discord": "Discord",
    "tiktok": "TikTok",
    "youtube": "YouTube",
}

CONTENT_STATUSES: tuple[str, ...] = (
    "DRAFT",
    "NEEDS_REVIEW",
    "APPROVED",
    "REJECTED",
    "PUBLISHED",
    "ARCHIVED",
)

# Allowed transitions (fail-closed outside this map).
STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "DRAFT": frozenset({"NEEDS_REVIEW", "ARCHIVED"}),
    "NEEDS_REVIEW": frozenset({"APPROVED", "REJECTED", "DRAFT", "ARCHIVED"}),
    "APPROVED": frozenset({"NEEDS_REVIEW", "ARCHIVED", "PUBLISHED"}),
    "REJECTED": frozenset({"DRAFT", "NEEDS_REVIEW", "ARCHIVED"}),
    "PUBLISHED": frozenset({"ARCHIVED"}),
    "ARCHIVED": frozenset({"DRAFT"}),
    # Legacy alias from earlier releases.
    "READY_FOR_REVIEW": frozenset({"APPROVED", "REJECTED", "DRAFT", "ARCHIVED", "NEEDS_REVIEW"}),
}


def normalize_status(status: str | None) -> str:
    raw = (status or "DRAFT").strip().upper()
    if raw == "READY_FOR_REVIEW":
        return "NEEDS_REVIEW"
    return raw if raw in CONTENT_STATUSES else "DRAFT"


def platform_catalog() -> list[dict[str, Any]]:
    return [
        {
            "id": pid,
            "label": {
                "facebook": "Facebook",
                "instagram": "Instagram",
                "telegram": "Telegram",
                "x": "X",
                "linkedin": "LinkedIn",
                "tiktok": "TikTok",
                "youtube_community": "YouTube Community",
                "discord": "Discord",
            }.get(pid, pid),
            "char_limit": PLATFORM_LIMITS[pid],
            "meta_destination": STUDIO_TO_META_DESTINATION.get(pid),
            "preview_only": pid not in STUDIO_TO_META_DESTINATION,
        }
        for pid in STUDIO_PLATFORMS
    ]
