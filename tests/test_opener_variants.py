"""Stable varied first-message openers: language, tone profile, banned phrases."""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from src.ai_agent.ai_client import _deterministic_draft, _empty_extracted_facts, _infer_facts_from_goal
from src.ai_agent.opener_variants import (
    _POOLS,
    build_first_opener_message,
    map_task_tone_to_opener_profile,
    resolve_opener_language,
    stable_opener_variant_index,
)


def _eff_buy_usdt():
    ex = _empty_extracted_facts()
    for k, v in _infer_facts_from_goal("buy USDT").items():
        if k in ex:
            ex[k] = v
    return ex


def test_map_tone_to_profile():
    assert map_task_tone_to_opener_profile("friendly") == "friendly"
    assert map_task_tone_to_opener_profile("professional") == "neutral"
    assert map_task_tone_to_opener_profile("strict") == "assertive"
    assert map_task_tone_to_opener_profile("aggressive_trader") == "aggressive_trader"


def test_resolve_language():
    assert resolve_opener_language("ru", "buy") == "ru"
    assert resolve_opener_language("hy", "buy") == "hy"
    assert resolve_opener_language("am", "buy") == "hy"
    assert resolve_opener_language("en", "buy") == "en"
    assert resolve_opener_language("auto", "купить usdt") == "ru"
    assert resolve_opener_language("auto", "Բարև USDT") == "hy"
    assert resolve_opener_language("auto", "buy usdt") == "en"


def test_same_task_stable_opener():
    eff = _eff_buy_usdt()
    task = SimpleNamespace(
        id=1001,
        tone="friendly",
        language="en",
    )
    a = build_first_opener_message(task=task, eff=eff, goal="buy USDT", target="@a", turn_number=0)
    b = build_first_opener_message(task=task, eff=eff, goal="buy USDT", target="@a", turn_number=0)
    assert a == b


def test_different_task_id_can_change_variant():
    eff = _eff_buy_usdt()
    texts = set()
    for tid in range(1, 80):
        task = SimpleNamespace(id=tid, tone="neutral", language="en")
        texts.add(
            build_first_opener_message(
                task=task, eff=eff, goal="buy USDT", target="@same", turn_number=0
            )
        )
    assert len(texts) >= 2


def test_russian_opener_has_cyrillic():
    eff = _eff_buy_usdt()
    task = SimpleNamespace(id=55, tone="friendly", language="ru")
    msg = build_first_opener_message(task=task, eff=eff, goal="buy USDT", target="@x", turn_number=0)
    assert re.search(r"[\u0400-\u04FF]", msg)
    assert "USDT" in msg


def test_armenian_opener_has_armenian_script():
    eff = _eff_buy_usdt()
    task = SimpleNamespace(id=56, tone="friendly", language="hy")
    msg = build_first_opener_message(task=task, eff=eff, goal="buy USDT", target="@x", turn_number=0)
    assert re.search(r"[\u0530-\u058F]", msg)
    assert "USDT" in msg


def test_banned_phrases_not_in_any_template():
    banned_sub = (
        "writing re:",
        "to move cleanly",
        "on our side",
        "to keep this practical",
    )
    for pool in _POOLS.values():
        for tmpl in pool:
            filled = tmpl.format(asset="USDT")
            low = filled.lower()
            for b in banned_sub:
                assert b not in low, (b, filled[:120])
            assert not re.search(r"\bhelps\b", low), filled[:120]


def test_known_buy_usdt_never_asks_side_or_which_asset():
    task = SimpleNamespace(id=77, tone="neutral", language="en")
    eff = _eff_buy_usdt()
    msg = build_first_opener_message(
        task=task, eff=eff, goal="buy USDT", target="@p", turn_number=0
    )
    low = msg.lower()
    assert "are you buying or selling" not in low
    assert "which asset" not in low
    assert "buy or sell" not in low


def test_buy_with_known_amount_asks_rate_not_volume():
    goal = "bay usdt erc20 2000 usdt"
    eff = _empty_extracted_facts()
    for k, v in _infer_facts_from_goal(goal).items():
        if k in eff:
            eff[k] = v
    task = SimpleNamespace(id=5001, tone="neutral", language="en")
    msg = build_first_opener_message(task=task, eff=eff, goal=goal, target="@s", turn_number=0)
    assert "2000" in msg
    assert "ERC20" in msg
    low = msg.lower()
    assert "rate" in low
    assert "volume" not in low
    assert "thank you for your interest" not in low
    assert "target premium" not in low


def test_friendly_index_zero_matches_pool_when_task_id_tuned():
    from src.ai_agent.opener_variants import _pick_pool

    goal = "we need to bay usdt"
    target = "@desk"
    pool = _pick_pool("en", "friendly", "buy_asset", _POOLS)
    want = pool[0].format(asset="USDT")
    tid = None
    for t in range(1, 50000):
        i = stable_opener_variant_index(
            task_id=t,
            target=target,
            goal=goal,
            turn_number=0,
            lang="en",
            profile="friendly",
            pool_len=len(pool),
        )
        if i == 0:
            tid = t
            break
    assert tid is not None
    task = SimpleNamespace(id=tid, tone="friendly", language="en")
    eff = _empty_extracted_facts()
    for k, v in _infer_facts_from_goal(goal).items():
        if k in eff:
            eff[k] = v
    msg = build_first_opener_message(
        task=task, eff=eff, goal=goal, target=target, turn_number=0
    )
    assert msg == want


def test_deterministic_draft_passes_task_to_opener():
    task = SimpleNamespace(
        id=901,
        goal_text="sell BTC",
        target_username_or_id="@q",
        negotiation_stage="opening",
        tone="assertive",
        language="en",
    )
    out = _deterministic_draft(task, [], None)
    assert out["extracted_facts"].get("side") == "sell"
    assert out["extracted_facts"].get("asset") == "BTC"
    assert "BTC" in out["draft_message"]
