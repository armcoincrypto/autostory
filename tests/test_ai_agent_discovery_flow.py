"""End-to-end discovery: deterministic brain, OpenAI bypass, no repeat asks."""
from __future__ import annotations

from types import SimpleNamespace

from src.ai_agent.ai_client import (
    _deterministic_body_from_profit,
    _deterministic_draft,
    _merge_base_facts,
)
from src.ai_agent.strategy import build_otc_policy_from_settings, merge_profit_strategy


def test_discovery_does_not_reask_payment_after_only_cash():
    goal = "we need to bay usdt get full information i have cash"
    task = SimpleNamespace(goal_text=goal)
    policy = build_otc_policy_from_settings()
    seller = "only cash +1%"
    eff = _merge_base_facts(None, task)
    eff = merge_profit_strategy(eff, policy=policy, latest_inbound_text=seller)
    hist = [{"role": "user", "content": seller}]
    body = _deterministic_body_from_profit(
        eff, policy, user_txt=seller, goal=goal, history=hist
    )
    low = body.lower()
    assert "which payment" not in low
    assert "payment method" not in low


def test_second_turn_does_not_repeat_location_when_in_history():
    goal = "we need to bay usdt get full information i have cash"
    task = SimpleNamespace(goal_text=goal)
    policy = build_otc_policy_from_settings()
    eff = _merge_base_facts(None, task)
    hist = [
        {"role": "user", "content": "only cash +1%"},
        {"role": "user", "content": "Kentron Yerevan, AMD only, 10:00"},
    ]
    from src.ai_agent.conversation_state import ingest_operational_from_history

    ingest_operational_from_history(eff, hist)
    eff = merge_profit_strategy(
        eff, policy=policy, latest_inbound_text="Kentron Yerevan, AMD only, 10:00"
    )
    body = _deterministic_body_from_profit(
        eff,
        policy,
        user_txt="Kentron Yerevan, AMD only, 10:00",
        goal=goal,
        history=hist,
    )
    low = body.lower()
    assert "central yerevan" not in low or body.count("?") <= 2
    assert "where works" not in low


def test_openai_path_bypassed_under_operational_discovery(monkeypatch):
    import json

    from config.settings import settings
    from src.ai_agent.ai_client import AiAgentClient

    class _Resp:
        def __init__(self) -> None:
            self._b = json.dumps(
                {"choices": [{"message": {"content": "SHOULD_NOT_APPEAR"}}]}
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def read(self):
            return self._b

    called = {"n": 0}

    def _fake_urlopen(req, timeout=30):
        called["n"] += 1
        return _Resp()

    monkeypatch.setattr("src.ai_agent.ai_client.urllib.request.urlopen", _fake_urlopen)
    cfg = settings.model_copy(
        update={
            "openai_api_key": "secret",
            "openai_model": "",
            "ai_agent_model": "gpt-test",
            "ai_agent_provider": "openai",
        }
    )
    goal = "we need to bay usdt get full information i have cash"
    task = SimpleNamespace(
        id=99,
        goal_text=goal,
        target_username_or_id="@x",
        tone="neutral",
        language="en",
    )
    hist = [{"role": "user", "content": "cash +1%"}]
    client = AiAgentClient(settings=cfg)
    out = client._openai_generate(task, hist, None)
    assert called["n"] == 0
    assert "SHOULD_NOT_APPEAR" not in out["draft_message"]
    assert out["meta"].get("openai_bypass") is True
    assert out["meta"]["phase"] == "discovery_hard"


def test_task_33_style_deterministic_draft_short():
    goal = "we need to bay usdt get full information i have cash"
    task = SimpleNamespace(
        id=33,
        goal_text=goal,
        target_username_or_id="@seller",
        tone="professional",
        language="en",
    )
    hist = [
        {
            "role": "assistant",
            "content": "Hi — we need to buy USDT with cash and want to understand your process.",
        },
        {"role": "user", "content": "I can sell only with cash +1%"},
    ]
    out = _deterministic_draft(task, hist, None)
    body = out["draft_message"]
    assert body.count("?") <= 2
    assert "got it, cash and +1%" in body.lower()
    assert "which payment method" not in body.lower()
    assert "closer to" not in body.lower()
