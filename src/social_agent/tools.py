"""Canonical Social Agent tool registry — UI and AI share these definitions."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Callable


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
            description="Translate draft text (requires AI provider when configured).",
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
            name="publishing.preview",
            description="Build a dry-run publish preview for selected destinations.",
            side_effect_class=SideEffectClass.READ_ONLY,
            required_permission="publishing.publish",
            confirmation_required=False,
            dry_run_support=True,
        ),
        ToolSpec(
            name="publishing.publish",
            description="Publish approved content to connected destinations.",
            side_effect_class=SideEffectClass.IMMEDIATE_EXTERNAL_MUTATION,
            required_permission="publishing.publish",
            confirmation_required=True,
            dry_run_support=True,
            available=False,
            unavailable_reason="Live publishing requires explicit canary authorization and provider connection.",
        ),
        ToolSpec(
            name="publishing.schedule",
            description="Schedule approved content.",
            side_effect_class=SideEffectClass.SCHEDULED_MUTATION,
            required_permission="publishing.schedule",
            confirmation_required=True,
            dry_run_support=True,
            available=False,
            unavailable_reason="Scheduler mutations remain locked until Social Agent scheduling certification.",
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
            name="analytics.get_summary",
            description="Get analytics summary.",
            side_effect_class=SideEffectClass.READ_ONLY,
            required_permission="analytics.view",
            confirmation_required=False,
            dry_run_support=True,
            available=False,
            unavailable_reason="Provider analytics not configured.",
        ),
        ToolSpec(
            name="brand.search_knowledge",
            description="Search Exswaping brand knowledge.",
            side_effect_class=SideEffectClass.READ_ONLY,
            required_permission="brand.manage",
            confirmation_required=False,
            dry_run_support=True,
        ),
        ToolSpec(
            name="exswaping.get_public_content",
            description="Fetch official Exswaping public content.",
            side_effect_class=SideEffectClass.READ_ONLY,
            required_permission="social_agent.view",
            confirmation_required=False,
            dry_run_support=True,
            available=False,
            unavailable_reason="Not configured — approved public-content API required.",
            provider_dependency="exswaping",
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
