"""AI Agent negotiation desk — draft engine and services (no Telegram in early phases)."""

from __future__ import annotations

from typing import Any

__all__ = ["AiAgentClient", "AiAgentService", "TelegramSingleSender"]


def __getattr__(name: str) -> Any:
    """Lazy exports so ``import src.ai_agent.<submodule>`` does not import Telethon/OpenAI stack."""
    if name == "AiAgentClient":
        from src.ai_agent.ai_client import AiAgentClient

        return AiAgentClient
    if name == "AiAgentService":
        from src.ai_agent.service import AiAgentService

        return AiAgentService
    if name == "TelegramSingleSender":
        from src.ai_agent.telegram_single_sender import TelegramSingleSender

        return TelegramSingleSender
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
