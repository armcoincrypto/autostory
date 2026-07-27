"""Canonical Social Agent tool registry — UI and AI share these definitions."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class SideEffectClass(str, Enum):
    READ_ONLY = "READ_ONLY"
    DRAFT_CREATION = "DRAFT_CREATION"
    MEDIA_GENERATION = "MEDIA_GENERATION"
    SCHEDULED_MUTATION = "SCHEDULED_MUTATION"
    IMMEDIATE_EXTERNAL_MUTATION = "IMMEDIATE_EXTERNAL_MUTATION"
    DESTRUCTIVE = "DESTRUCTIVE"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    side_effect_class: SideEffectClass
    required_permission: str
    confirmation_required: bool
    dry_run_support: bool
    provider_dependency: str | None = None
    available: bool = True
    unavailable_reason: str | None = None
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)


def _tools() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="social.list_connections",
            description="List social connections and health.",
            side_effect_class=SideEffectClass.READ_ONLY,
            required_permission="social_agent.view",
            confirmation_required=False,
            dry_run_support=True,
        ),
        ToolSpec(
            name="social.get_connection_health",
            description="Get health for one social connection.",
            side_effect_class=SideEffectClass.READ_ONLY,
            required_permission="social_agent.view",
            confirmation_required=False,
            dry_run_support=True,
            input_schema={"connection_id": "string"},
        ),
        ToolSpec(
            name="content.create_draft",
            description="Create a content draft with optional platform variants.",
            side_effect_class=SideEffectClass.DRAFT_CREATION,
            required_permission="content.create",
            confirmation_required=False,
            dry_run_support=True,
            input_schema={"title": "string", "body": "string", "platforms": "string[]"},
        ),
        ToolSpec(
            name="content.generate_variants",
            description="Generate platform variants from a brief (local template when AI unset).",
            side_effect_class=SideEffectClass.DRAFT_CREATION,
            required_permission="content.create",
            confirmation_required=False,
            dry_run_support=True,
            input_schema={"brief": "string", "platforms": "string[]", "languages": "string[]"},
        ),
        ToolSpec(
            name="content.translate",
            description="Translate draft text (local stub until AI translation certified).",
            side_effect_class=SideEffectClass.DRAFT_CREATION,
            required_permission="content.edit",
            confirmation_required=False,
            dry_run_support=True,
            provider_dependency="ai",
        ),
        ToolSpec(
            name="content.rewrite",
            description="Rewrite draft tone/length.",
            side_effect_class=SideEffectClass.DRAFT_CREATION,
            required_permission="content.edit",
            confirmation_required=False,
            dry_run_support=True,
            provider_dependency="ai",
        ),
        ToolSpec(
            name="content.transition_status",
            description="Move content through Draft → Needs Review → Approved/Rejected/Archived.",
            side_effect_class=SideEffectClass.DRAFT_CREATION,
            required_permission="content.approve",
            confirmation_required=False,
            dry_run_support=True,
            input_schema={"content_id": "number", "status": "string", "note": "string?"},
        ),
        ToolSpec(
            name="content.studio_preview",
            description="Render multi-platform Content Studio previews via canonical renderers.",
            side_effect_class=SideEffectClass.READ_ONLY,
            required_permission="content.edit",
            confirmation_required=False,
            dry_run_support=True,
            input_schema={"content_id": "number"},
        ),
        ToolSpec(
            name="copilot.assist",
            description="Social copilot helpers (rewrite, hashtags, compliance, calendar). Never publishes.",
            side_effect_class=SideEffectClass.READ_ONLY,
            required_permission="social_agent.chat",
            confirmation_required=False,
            dry_run_support=True,
            input_schema={"intent": "string", "text": "string?", "platform": "string?"},
        ),
        ToolSpec(
            name="publishing.dry_run",
            description="Validate content and render Meta provider payloads without calling Graph API.",
            side_effect_class=SideEffectClass.READ_ONLY,
            required_permission="publishing.publish",
            confirmation_required=False,
            dry_run_support=True,
            input_schema={
                "destinations": "string[]",
                "content_id": "number?",
                "content": "object?",
            },
        ),
        ToolSpec(
            name="publishing.preview",
            description="Alias for publishing.dry_run — build payload previews only.",
            side_effect_class=SideEffectClass.READ_ONLY,
            required_permission="publishing.publish",
            confirmation_required=False,
            dry_run_support=True,
            input_schema={
                "destinations": "string[]",
                "content_id": "number?",
                "content": "object?",
            },
        ),
        ToolSpec(
            name="publishing.facebook_canary",
            description=(
                "Controlled Facebook Page canary: dry-run → CONFIRM → single-use auth → one Graph POST. "
                "Instagram and general live publishing remain unavailable."
            ),
            side_effect_class=SideEffectClass.IMMEDIATE_EXTERNAL_MUTATION,
            required_permission="publishing.publish",
            confirmation_required=True,
            dry_run_support=True,
            provider_dependency="meta",
            available=True,
            input_schema={
                "action": "prepare|preflight|authorize|execute",
                "explicit_approval": "CONFIRM?",
                "authorization_id": "number?",
                "authorization_secret": "string?",
                "dry_run_id": "number?",
                "payload_hash": "string?",
                "idempotency_key": "string?",
            },
        ),
        ToolSpec(
            name="publishing.publish",
            description="Publish approved content to connected destinations.",
            side_effect_class=SideEffectClass.IMMEDIATE_EXTERNAL_MUTATION,
            required_permission="publishing.publish",
            confirmation_required=True,
            dry_run_support=True,
            available=False,
            unavailable_reason="General live publishing is disabled. Use publishing.facebook_canary for the controlled canary only.",
        ),
        ToolSpec(
            name="publishing.schedule",
            description="Schedule approved content for live publishing.",
            side_effect_class=SideEffectClass.SCHEDULED_MUTATION,
            required_permission="publishing.schedule",
            confirmation_required=True,
            dry_run_support=True,
            available=False,
            unavailable_reason="Live scheduler mutations remain locked. Use calendar.queue for preview queue only.",
        ),
        ToolSpec(
            name="calendar.list",
            description="List calendar entries and scheduled queue (no live publish).",
            side_effect_class=SideEffectClass.READ_ONLY,
            required_permission="publishing.schedule",
            confirmation_required=False,
            dry_run_support=True,
        ),
        ToolSpec(
            name="calendar.queue",
            description="Queue content on the calendar. Does not enable live publishing.",
            side_effect_class=SideEffectClass.SCHEDULED_MUTATION,
            required_permission="publishing.schedule",
            confirmation_required=False,
            dry_run_support=True,
            input_schema={
                "content_id": "number",
                "platform": "string",
                "scheduled_for": "string",
                "timezone": "string?",
            },
        ),
        ToolSpec(
            name="media.list_assets",
            description="List media library assets.",
            side_effect_class=SideEffectClass.READ_ONLY,
            required_permission="media.manage",
            confirmation_required=False,
            dry_run_support=True,
        ),
        ToolSpec(
            name="media.create_folder",
            description="Create a media library folder.",
            side_effect_class=SideEffectClass.DRAFT_CREATION,
            required_permission="media.manage",
            confirmation_required=False,
            dry_run_support=True,
            input_schema={"name": "string", "parent_id": "number?"},
        ),
        ToolSpec(
            name="analytics.get_summary",
            description="Analytics architecture summary. Returns no fabricated metrics.",
            side_effect_class=SideEffectClass.READ_ONLY,
            required_permission="analytics.view",
            confirmation_required=False,
            dry_run_support=True,
            available=True,
        ),
        ToolSpec(
            name="comments.list",
            description="Comments architecture — no polling, no fake comments.",
            side_effect_class=SideEffectClass.READ_ONLY,
            required_permission="comments.reply",
            confirmation_required=False,
            dry_run_support=True,
        ),
        ToolSpec(
            name="messages.list",
            description="Messages inbox architecture — no provider writes.",
            side_effect_class=SideEffectClass.READ_ONLY,
            required_permission="messages.reply",
            confirmation_required=False,
            dry_run_support=True,
        ),
        ToolSpec(
            name="automations.list",
            description="List automation workflow drafts (always disabled).",
            side_effect_class=SideEffectClass.READ_ONLY,
            required_permission="automations.manage",
            confirmation_required=False,
            dry_run_support=True,
        ),
        ToolSpec(
            name="brand.search_knowledge",
            description="Search brand knowledge.",
            side_effect_class=SideEffectClass.READ_ONLY,
            required_permission="brand.manage",
            confirmation_required=False,
            dry_run_support=True,
        ),
        ToolSpec(
            name="brand.list_knowledge",
            description="List all brand knowledge entries.",
            side_effect_class=SideEffectClass.READ_ONLY,
            required_permission="brand.manage",
            confirmation_required=False,
            dry_run_support=True,
        ),
        ToolSpec(
            name="brand.upsert",
            description="Create or update a brand knowledge entry.",
            side_effect_class=SideEffectClass.DRAFT_CREATION,
            required_permission="brand.manage",
            confirmation_required=False,
            dry_run_support=True,
            input_schema={"category": "string", "key": "string", "title": "string", "value": "string"},
        ),
    ]


def list_tools(*, include_unavailable: bool = True) -> list[dict[str, Any]]:
    out = []
    for t in _tools():
        if not include_unavailable and not t.available:
            continue
        d = asdict(t)
        d["side_effect_class"] = t.side_effect_class.value
        out.append(d)
    return out


def get_tool(name: str) -> ToolSpec | None:
    for t in _tools():
        if t.name == name:
            return t
    return None
