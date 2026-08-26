"""Phase 1.1 — Story mutation boundary fail-closed regression tests."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.stories.mutation_boundary import (
    StoryExecutionMode,
    StoryMutationAuthorization,
    StoryMutationService,
    StoryMutationTrigger,
    clear_mutation_token_state_for_tests,
    get_provider_call_count,
    invoke_send_story,
    parse_story_execution_mode,
    require_story_mutation_authorization,
    story_account_mutation_allowed,
    story_mutations_enabled,
)


@pytest.fixture(autouse=True)
def _reset_boundary(monkeypatch):
    clear_mutation_token_state_for_tests()
    monkeypatch.delenv("STORY_MUTATIONS_ENABLED", raising=False)
    monkeypatch.delenv("STORY_EXECUTION_MODE", raising=False)
    monkeypatch.delenv("STORY_ACCOUNT_MUTATION_ALLOWLIST", raising=False)
    monkeypatch.delenv("CONTROLLED_STORY_EXECUTION_ENABLED", raising=False)
    monkeypatch.delenv("CONTROLLED_STORY_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("SCHEDULER_STORY_EXECUTION_ENABLED", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    yield
    clear_mutation_token_state_for_tests()


def test_global_kill_switch_defaults_deny(monkeypatch) -> None:
    monkeypatch.delenv("STORY_MUTATIONS_ENABLED", raising=False)
    assert story_mutations_enabled() is False
    monkeypatch.setenv("STORY_MUTATIONS_ENABLED", "false")
    assert story_mutations_enabled() is False
    monkeypatch.setenv("STORY_MUTATIONS_ENABLED", "maybe")
    assert story_mutations_enabled() is False
    monkeypatch.setenv("STORY_MUTATIONS_ENABLED", "")
    assert story_mutations_enabled() is False


def test_incident_reproduction_controlled_false_zero_provider_calls(monkeypatch) -> None:
    """Reproduce reported incident config: CONTROLLED=false → no provider calls."""
    monkeypatch.setenv("CONTROLLED_STORY_EXECUTION_ENABLED", "false")
    monkeypatch.setenv("CONTROLLED_STORY_ACCOUNT_ID", "140")
    monkeypatch.delenv("STORY_MUTATIONS_ENABLED", raising=False)

    decision = StoryMutationService.evaluate(
        account_id=140,
        trigger=StoryMutationTrigger.CANARY,
        scope="controlled_live",
        caller="incident_repro",
    )
    assert decision.allowed is False
    assert decision.denial_reason == "story_mutations_disabled"
    assert get_provider_call_count() == 0


def test_phase06_canary_pattern_blocked_without_global_switch(monkeypatch) -> None:
    """Phase 0.6 briefly set CONTROLLED=true; without STORY_MUTATIONS_ENABLED still deny."""
    monkeypatch.setenv("CONTROLLED_STORY_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("CONTROLLED_STORY_ACCOUNT_ID", "140")
    monkeypatch.setenv("STORY_EXECUTION_MODE", "controlled-canary")
    monkeypatch.setenv("STORY_ACCOUNT_MUTATION_ALLOWLIST", "140")
    monkeypatch.setenv("STORY_MUTATIONS_ENABLED", "false")

    from src.core.execution_guard import ACTION_STORY_PUBLISH, can_execute_action

    guard = can_execute_action(
        ACTION_STORY_PUBLISH,
        account_id=140,
        scope="controlled_live",
        skip_audit=True,
    )
    assert guard.allowed is False
    assert guard.reason_code == "story_mutations_disabled"

    decision = require_story_mutation_authorization(
        account_id=140,
        scope="controlled_live",
        trigger=StoryMutationTrigger.CANARY.value,
        caller="phase06_repro",
    )
    assert decision.allowed is False
    assert get_provider_call_count() == 0


def test_account_allowlist_default_deny(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("STORY_MUTATIONS_ENABLED", "true")
    monkeypatch.setenv("STORY_EXECUTION_MODE", "controlled-canary")
    monkeypatch.delenv("STORY_ACCOUNT_MUTATION_ALLOWLIST", raising=False)
    ok, reason = story_account_mutation_allowed(140)
    assert ok is False
    assert reason == "account_mutation_allowlist_empty"

    monkeypatch.setenv("STORY_ACCOUNT_MUTATION_ALLOWLIST", "999")
    ok, reason = story_account_mutation_allowed(140)
    assert ok is False
    assert reason == "account_mutation_denied"


def test_execution_mode_unknown_disables(monkeypatch) -> None:
    monkeypatch.setenv("STORY_EXECUTION_MODE", "wat")
    assert parse_story_execution_mode() == StoryExecutionMode.DISABLED


def test_provider_boundary_rejects_missing_forged_expired_reused(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("STORY_MUTATIONS_ENABLED", "true")
    monkeypatch.setenv("STORY_EXECUTION_MODE", "controlled-canary")
    monkeypatch.setenv("STORY_ACCOUNT_MUTATION_ALLOWLIST", "140")
    monkeypatch.setenv("CONTROLLED_STORY_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("CONTROLLED_STORY_ACCOUNT_ID", "140")

    ok, reason = StoryMutationService.consume_provider_authorization(None, account_id=140)
    assert ok is False and reason == "provider_authorization_missing"

    decision = StoryMutationService.evaluate(
        account_id=140,
        trigger=StoryMutationTrigger.CANARY,
        scope="controlled_live",
        caller="token_tests",
    )
    assert decision.allowed and decision.authorization

    forged = StoryMutationAuthorization(
        token_id=decision.authorization.token_id,
        account_id=140,
        trigger="canary",
        execution_mode="controlled-canary",
        scope="controlled_live",
        issued_at_unix=decision.authorization.issued_at_unix,
        expires_at_unix=decision.authorization.expires_at_unix,
        signature="0" * 64,
    )
    ok, reason = StoryMutationService.consume_provider_authorization(forged, account_id=140)
    assert ok is False and reason == "provider_authorization_forged"

    expired = StoryMutationAuthorization(
        token_id="deadbeef",
        account_id=140,
        trigger="canary",
        execution_mode="controlled-canary",
        scope="controlled_live",
        issued_at_unix=0,
        expires_at_unix=1,
        signature="x",
    )
    ok, reason = StoryMutationService.consume_provider_authorization(expired, account_id=140)
    assert ok is False and reason == "provider_authorization_expired"

    # Valid consume then reuse
    ok, reason = StoryMutationService.consume_provider_authorization(
        decision.authorization, account_id=140
    )
    assert ok is True
    ok, reason = StoryMutationService.consume_provider_authorization(
        decision.authorization, account_id=140
    )
    assert ok is False and reason == "provider_authorization_reused"


def test_queued_after_disable_rejects(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("STORY_MUTATIONS_ENABLED", "true")
    monkeypatch.setenv("STORY_EXECUTION_MODE", "controlled-canary")
    monkeypatch.setenv("STORY_ACCOUNT_MUTATION_ALLOWLIST", "140")
    monkeypatch.setenv("CONTROLLED_STORY_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("CONTROLLED_STORY_ACCOUNT_ID", "140")

    decision = StoryMutationService.evaluate(
        account_id=140,
        trigger=StoryMutationTrigger.CANARY,
        scope="controlled_live",
        caller="queue_recheck",
    )
    assert decision.authorization is not None

    monkeypatch.setenv("STORY_MUTATIONS_ENABLED", "false")
    ok, reason = StoryMutationService.consume_provider_authorization(
        decision.authorization, account_id=140
    )
    assert ok is False
    assert reason == "story_mutations_disabled_at_provider"


def test_invoke_send_story_increments_counter_only_when_authorized(monkeypatch) -> None:
    from telethon.tl.functions.stories import SendStoryRequest
    from src.stories import mutation_boundary as mb

    class _FakeClient:
        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self, request):
            self.calls += 1
            return SimpleNamespace(ok=True)

    class _FakeSendStoryRequest:
        pass

    client = _FakeClient()
    monkeypatch.setattr(
        "telethon.tl.functions.stories.SendStoryRequest",
        _FakeSendStoryRequest,
    )

    async def _deny():
        with pytest.raises(PermissionError):
            await invoke_send_story(
                client,
                _FakeSendStoryRequest(),
                authorization=None,
                account_id=140,
            )

    asyncio.run(_deny())
    assert client.calls == 0
    assert get_provider_call_count() == 0

    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("STORY_MUTATIONS_ENABLED", "true")
    monkeypatch.setenv("STORY_EXECUTION_MODE", "controlled-canary")
    monkeypatch.setenv("STORY_ACCOUNT_MUTATION_ALLOWLIST", "140")
    monkeypatch.setenv("CONTROLLED_STORY_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("CONTROLLED_STORY_ACCOUNT_ID", "140")
    decision = StoryMutationService.evaluate(
        account_id=140,
        trigger=StoryMutationTrigger.CANARY,
        scope="controlled_live",
        caller="invoke_test",
    )
    assert decision.authorization is not None

    # Patch the type check inside invoke_send_story to accept our fake request.
    async def _allow():
        import telethon.tl.functions.stories as stories_fn

        real_type = stories_fn.SendStoryRequest
        stories_fn.SendStoryRequest = _FakeSendStoryRequest  # type: ignore[misc,assignment]
        try:
            return await invoke_send_story(
                client,
                _FakeSendStoryRequest(),
                authorization=decision.authorization,
                account_id=140,
            )
        finally:
            stories_fn.SendStoryRequest = real_type  # type: ignore[misc,assignment]

    asyncio.run(_allow())
    assert client.calls == 1
    assert get_provider_call_count() == 1


def test_dry_run_decision_never_issues_provider_token(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("STORY_MUTATIONS_ENABLED", "true")
    monkeypatch.setenv("STORY_EXECUTION_MODE", "dry-run")
    monkeypatch.setenv("STORY_ACCOUNT_MUTATION_ALLOWLIST", "140")
    decision = StoryMutationService.evaluate(
        account_id=140,
        trigger=StoryMutationTrigger.CANARY,
        scope="controlled_live",
        dry_run=True,
        caller="dry_run",
    )
    assert decision.decision == "dry-run"
    assert decision.authorization is None
    assert decision.provider_called is False
    assert get_provider_call_count() == 0


def test_scheduler_and_automation_triggers_denied(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("STORY_MUTATIONS_ENABLED", "true")
    monkeypatch.setenv("STORY_EXECUTION_MODE", "live")
    monkeypatch.setenv("STORY_ACCOUNT_MUTATION_ALLOWLIST", "140")
    for trigger in (
        StoryMutationTrigger.SCHEDULER,
        StoryMutationTrigger.AUTOMATION,
        StoryMutationTrigger.RECOVERY,
    ):
        decision = StoryMutationService.evaluate(
            account_id=140,
            trigger=trigger,
            scope="controlled_live",
            caller="trigger_test",
            skip_legacy_purpose_check=True,
        )
        assert decision.allowed is False


def test_guard_requires_purpose_when_global_enabled(monkeypatch) -> None:
    from src.core.execution_guard import ACTION_STORY_PUBLISH, can_execute_action

    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("STORY_MUTATIONS_ENABLED", "true")
    monkeypatch.setenv("STORY_EXECUTION_MODE", "controlled-canary")
    monkeypatch.setenv("STORY_ACCOUNT_MUTATION_ALLOWLIST", "140")
    monkeypatch.setenv("CONTROLLED_STORY_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("CONTROLLED_STORY_ACCOUNT_ID", "140")
    decision = can_execute_action(
        ACTION_STORY_PUBLISH,
        account_id=140,
        skip_audit=True,
    )
    assert decision.allowed is False
    assert decision.reason_code == "story_execution_purpose_required"
