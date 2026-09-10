"""Thin platform agent launcher registry (not a plugin marketplace).

Wave J: Agents hub lists only agent workspaces — not duplicate primary products
(Broadcast) and not Advanced-only tools (AI Coding).
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
        agent_id="social_agent",
        display_name="Social Agent",
        route="/social-agent",
        description="Social content, social accounts, publishing preview, and AI assistant (not Telegram Messages).",
        status="active",
        owner="storyfleet",
        frontend_component="social_agent/overview.html",
        backend_handler="src.dashboard.social_agent_routes",
    ),
    PlatformAgent(
        agent_id="ai_agent",
        display_name="AI Agent",
        route="/ai-agent",
        description="Legacy Telegram negotiation desk for reserved AI accounts. Owner UI retired; history preserved.",
        status="inactive",
        owner="storyfleet",
        frontend_component="ai_agent.html",
        backend_handler="src.dashboard.ai_agent_routes",
    ),
)


def list_platform_agents() -> list[dict[str, Any]]:
    return [asdict(a) for a in _AGENTS]


def get_agent(agent_id: str) -> dict[str, Any] | None:
    for a in _AGENTS:
        if a.agent_id == agent_id:
            return asdict(a)
    return None
