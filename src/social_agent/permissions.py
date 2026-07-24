"""Permission helpers for Social Agent (backend enforcement)."""
from __future__ import annotations

import os

# First release: dashboard admins get the full Social Agent permission set.
# Granular RBAC can map these later without changing service call sites.
ALL_PERMISSIONS = frozenset(
    {
        "social_agent.view",
        "social_agent.chat",
        "social_accounts.manage",
        "content.create",
        "content.edit",
        "content.approve",
        "publishing.publish",
        "publishing.schedule",
        "media.manage",
        "analytics.view",
        "comments.reply",
        "messages.reply",
        "brand.manage",
        "automations.manage",
        "settings.manage",
        "audit.view",
    }
)


def actor_permissions(*, is_dashboard_admin: bool) -> set[str]:
    if is_dashboard_admin:
        return set(ALL_PERMISSIONS)
    # Token-only callers treated as admin for this phase when DASHBOARD_ADMIN_TOKEN used.
    if os.environ.get("SOCIAL_AGENT_TOKEN_IS_ADMIN", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return set(ALL_PERMISSIONS)
    return {"social_agent.view"}


def require_permission(perms: set[str], needed: str) -> tuple[bool, str | None]:
    if needed in perms:
        return True, None
    return False, f"missing_permission:{needed}"
