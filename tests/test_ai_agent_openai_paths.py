"""OpenAI draft path (mocked HTTP)."""
from __future__ import annotations

import json
from types import SimpleNamespace

from config.settings import settings
from src.ai_agent.ai_client import AiAgentClient


class _Resp:
    def __init__(self, payload: dict) -> None:
        self._b = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *a) -> None:
        return None

    def read(self) -> bytes:
        return self._b


def test_openai_generate_returns_model_text(monkeypatch):
    task = SimpleNamespace(
        id=1,
        goal_text="buy USDT",
        target_username_or_id="@x",
        tone="neutral",
        language="en",
    )
    cfg = settings.model_copy(
        update={
            "openai_api_key": "secret",
            "openai_model": "",
            "ai_agent_model": "gpt-test",
        }
    )
    payload = {"choices": [{"message": {"content": "Hello from model"}}]}

    def _fake_urlopen(req, timeout=30):
        return _Resp(payload)

    monkeypatch.setattr("src.ai_agent.ai_client.urllib.request.urlopen", _fake_urlopen)
    client = AiAgentClient(settings=cfg)
    out = client._openai_generate(task, [], None)
    assert out["draft_message"] == "Hello from model"
    assert out["meta"]["provider"] == "openai"


def test_openai_errors_fall_back(monkeypatch):
    task = SimpleNamespace(
        id=2,
        goal_text="sell BTC",
        target_username_or_id="@y",
        tone="neutral",
        language="en",
    )
    cfg = settings.model_copy(
        update={
            "openai_api_key": "secret",
            "ai_agent_provider": "openai",
            "openai_model": "",
            "ai_agent_model": "gpt-test",
        }
    )

    def boom(*a, **k):
        raise OSError("network")

    monkeypatch.setattr("src.ai_agent.ai_client.urllib.request.urlopen", boom)
    client = AiAgentClient(settings=cfg)
    monkeypatch.setattr("src.ai_agent.ai_client._use_openai", lambda c: True)
    out = client.generate_draft(None, task, [], None)
    assert out["meta"]["provider"] == "deterministic"
