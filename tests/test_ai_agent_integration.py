"""Shared mocks for AI Agent service tests."""
from __future__ import annotations

from typing import Any, Optional

from src.ai_agent.ai_client import AiAgentClient


class MockSender:
    def __init__(self, fetch_messages: Optional[list[dict[str, Any]]] = None) -> None:
        self.fetch_messages = fetch_messages or []
        self.send_calls: list[dict[str, Any]] = []
        self._mid = 1

    def fetch_recent_messages(self, account_id: int, target: str, limit: int = 20) -> dict[str, Any]:
        return {
            "ok": True,
            "messages": list(self.fetch_messages),
            "error_code": None,
            "error_message": None,
        }

    def send_message(self, account_id: int, target: str, text: str) -> dict[str, Any]:
        self.send_calls.append(
            {"account_id": account_id, "target": target, "text": text}
        )
        mid = self._mid
        self._mid += 1
        return {
            "ok": True,
            "telegram_message_id": mid,
            "error_code": None,
            "error_message": None,
        }


class MockAiClient(AiAgentClient):
    """Deterministic client for tests (same as default provider)."""

    def generate_draft(
        self,
        db: Any,
        task: Any,
        history: list[Any],
        latest_facts: Optional[dict[str, Any]],
    ) -> dict[str, Any]:
        from src.ai_agent.ai_client import _deterministic_draft

        return _deterministic_draft(task, history, latest_facts)
