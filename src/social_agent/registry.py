"""Thin platform agent launcher registry (not a plugin marketplace).

STORYFLEET historically used flat sidebar workspaces. This registry makes
agent-like products first-class and discoverable without inventing a second app.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class PlatformAgent:
    agent_id: str
    display_name: str
    route: str
    description: str
    status: str  # active | limited | coming_later | inactive
    owner: str
    frontend_component: str
    backend_handler: str


_AGENTS: tuple[PlatformAgent, ...] = (
    PlatformAgent(
        agent_id="ai_agent",
        display_name="AI Agent",
        route="/ai-agent",
        description="Telegram negotiation desk for reserved AI accounts. Page route temporarily unavailable (Wave 1).",
        status="inactive",
        owner="storyfleet",
        frontend_component="ai_agent.html",
        backend_handler="src.dashboard.ai_agent_routes",
    ),
    PlatformAgent(
        agent_id="ai_coding",
        display_name="AI Coding",
        route="/ai-coding",
        description="Software factory review and execution transparency.",
        status="limited",
        owner="storyfleet",
        frontend_component="ai_coding.html",
        backend_handler="src.dashboard.ai_coding_routes",
    ),
    PlatformAgent(
        agent_id="broadcast",
        display_name="Broadcast",
        route="/broadcast",
        description="Broadcast / distribution workspace.",
        status="active",
        owner="storyfleet",
        frontend_component="broadcast.html",
        backend_handler="src.dashboard.broadcast_routes",
    ),
    PlatformAgent(
        agent_id="social_agent",
        display_name="Social Agent",
        route="/social-agent",
        description="Unified social content, accounts, publishing, and AI assistant.",
        status="active",
        owner="storyfleet",
        frontend_component="social_agent/overview.html",
        backend_handler="src.dashboard.social_agent_routes",
    ),
)


def list_platform_agents() -> list[dict[str, Any]]:
    return [asdict(a) for a in _AGENTS]


def get_agent(agent_id: str) -> dict[str, Any] | None:
    for a in _AGENTS:
        if a.agent_id == agent_id:
            return asdict(a)
    return None
