"""Multi-account Stories gates, mention allocation, and allowlist helpers."""
from __future__ import annotations

from src.stories.mutation_boundary import (
    parse_story_mutation_allowlist,
    story_accounts_mutation_allowed,
)
from src.stories.rotation_audit import (
    MULTI_ACCOUNT_CONFIRMATION_TOKEN,
    allocate_mentions_without_replacement,
    live_confirmation_token_for_accounts,
    live_gate_allowed,
)


def test_allocate_mentions_without_replacement_no_reuse() -> None:
    candidates = [{"user_id": i, "username": f"u{i}"} for i in range(1, 7)]
    allocated = allocate_mentions_without_replacement(
        candidates,
        account_ids=[106, 140, 108],
        mentions_per_story=2,
    )
    assert len(allocated) == 3
    assert [c["user_id"] for c in allocated[0]] == [1, 2]
    assert [c["user_id"] for c in allocated[1]] == [3, 4]
    assert [c["user_id"] for c in allocated[2]] == [5, 6]
    flat = [c["user_id"] for chunk in allocated for c in chunk]
    assert len(flat) == len(set(flat))


def test_allocate_mentions_zero_mentions() -> None:
    allocated = allocate_mentions_without_replacement(
        [{"user_id": 1}],
        account_ids=[106, 140],
        mentions_per_story=0,
    )
    assert allocated == [[], []]


def test_live_confirmation_token_multi() -> None:
    assert live_confirmation_token_for_accounts([140]) == "LIVE_STORY_ACCOUNT_140"
    assert live_confirmation_token_for_accounts([106, 140]) == MULTI_ACCOUNT_CONFIRMATION_TOKEN


def test_live_gate_allowlist_multi(monkeypatch) -> None:
    monkeypatch.setenv("STORY_ACCOUNT_MUTATION_ALLOWLIST", "106,140")
    ok, blockers = live_gate_allowed(
        {
            "explicit_operator_approval": True,
            "confirmation_token": MULTI_ACCOUNT_CONFIRMATION_TOKEN,
        },
        {"eligible_accounts": [106, 140], "live_blockers": [], "live_only_blockers": []},
    )
    assert ok is True
    assert blockers == []


def test_live_gate_rejects_non_allowlisted(monkeypatch) -> None:
    monkeypatch.setenv("STORY_ACCOUNT_MUTATION_ALLOWLIST", "140")
    ok, blockers = live_gate_allowed(
        {
            "explicit_operator_approval": True,
            "confirmation_token": MULTI_ACCOUNT_CONFIRMATION_TOKEN,
        },
        {"eligible_accounts": [106, 140], "live_blockers": [], "live_only_blockers": []},
    )
    assert ok is False
    assert "live_gate_accounts_not_on_allowlist" in blockers


def test_parse_allowlist(monkeypatch) -> None:
    monkeypatch.setenv("STORY_ACCOUNT_MUTATION_ALLOWLIST", "106, 140,")
    assert parse_story_mutation_allowlist() == {106, 140}
    ok, reason, denied = story_accounts_mutation_allowed([106, 999])
    assert ok is False
    assert reason == "account_mutation_denied"
    assert denied == [999]
