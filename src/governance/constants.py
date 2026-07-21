"""Canonical runtime governance role and tag names."""
from __future__ import annotations

# Hard safety roles (fallback may also inject these)
SYSTEM_PROTECTED = "SYSTEM_PROTECTED"
AI_RESERVED = "AI_RESERVED"
MANUAL_ONLY = "MANUAL_ONLY"
QUARANTINED = "QUARANTINED"

# Capability roles (operator-assigned; never override hard fallback alone)
STORY_ALLOWED = "STORY_ALLOWED"
DISCOVERY_ALLOWED = "DISCOVERY_ALLOWED"
SCHEDULER_ALLOWED = "SCHEDULER_ALLOWED"
LIVE_ALLOWED = "LIVE_ALLOWED"

ALL_ROLES = frozenset(
    {
        SYSTEM_PROTECTED,
        AI_RESERVED,
        MANUAL_ONLY,
        QUARANTINED,
        STORY_ALLOWED,
        DISCOVERY_ALLOWED,
        SCHEDULER_ALLOWED,
        LIVE_ALLOWED,
    }
)

ROLES_REQUIRING_REASON = frozenset({SYSTEM_PROTECTED, QUARANTINED, MANUAL_ONLY})

ROLE_BADGE_META: dict[str, dict[str, str]] = {
    SYSTEM_PROTECTED: {"label": "SYSTEM", "tone": "danger", "title": "System protected — no auto-use"},
    AI_RESERVED: {"label": "AI", "tone": "reserved", "title": "Reserved for AI agent desk"},
    MANUAL_ONLY: {"label": "MANUAL", "tone": "warning", "title": "Manual operator action required"},
    QUARANTINED: {"label": "QUARANTINED", "tone": "danger", "title": "Quarantined from runtime use"},
    STORY_ALLOWED: {"label": "STORY", "tone": "success", "title": "Story runtime role assigned"},
    DISCOVERY_ALLOWED: {"label": "DISCOVERY", "tone": "info", "title": "Discovery runtime role assigned"},
    SCHEDULER_ALLOWED: {"label": "SCHEDULER", "tone": "info", "title": "Scheduler runtime role assigned"},
    LIVE_ALLOWED: {"label": "LIVE", "tone": "warning", "title": "Live story path role assigned"},
}
