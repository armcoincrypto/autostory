"""Renderer interface — preview payloads only."""
from __future__ import annotations

from typing import Any, Protocol

from src.social_agent.publishing.content import PublishContent


class DestinationRenderer(Protocol):
    destination: str

    def render(
        self,
        content: PublishContent,
        *,
        connection: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Return a provider payload preview. Must never perform HTTP."""
        ...
