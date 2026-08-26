"""Fail-closed local/dev Story publishing and /debug hygiene (no Telegram)."""
from __future__ import annotations

import json

from src.stories.mutation_boundary import (
    StoryMutationService,
    StoryMutationTrigger,
    get_provider_call_count,
    reset_provider_call_counter,
    story_mutations_enabled,
)


def _live_flags(monkeypatch, *, environment: str | None) -> None:
    if environment is None:
        monkeypatch.delenv("ENVIRONMENT", raising=False)
    else:
        monkeypatch.setenv("ENVIRONMENT", environment)
    monkeypatch.setenv("STORY_MUTATIONS_ENABLED", "true")
    monkeypatch.setenv("CAMPAIGN_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("CONTROLLED_STORY_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("SCHEDULER_STORY_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("STORY_EXECUTION_MODE", "live")
    monkeypatch.setenv("STORY_ACCOUNT_MUTATION_ALLOWLIST", "1")
    monkeypatch.setenv("CONTROLLED_STORY_ACCOUNT_ID", "1")


def _evaluate():
    reset_provider_call_counter()
    return StoryMutationService.evaluate(
        account_id=1,
        trigger=StoryMutationTrigger.OPERATOR,
        scope="controlled_live",
        caller="local_safety",
        skip_legacy_purpose_check=True,
    )


def test_development_cannot_publish(monkeypatch) -> None:
    _live_flags(monkeypatch, environment="development")
    assert story_mutations_enabled() is False
    decision = _evaluate()
    assert decision.allowed is False
    assert decision.denial_reason == "story_mutations_disabled"
    assert get_provider_call_count() == 0


def test_test_env_cannot_publish(monkeypatch) -> None:
    _live_flags(monkeypatch, environment="test")
    assert story_mutations_enabled() is False
    decision = _evaluate()
    assert decision.allowed is False
    assert get_provider_call_count() == 0


def test_missing_env_cannot_publish(monkeypatch) -> None:
    _live_flags(monkeypatch, environment=None)
    assert story_mutations_enabled() is False
    decision = _evaluate()
    assert decision.allowed is False
    assert get_provider_call_count() == 0


def test_production_incomplete_flags_cannot_publish(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("STORY_MUTATIONS_ENABLED", "true")
    monkeypatch.setenv("CAMPAIGN_EXECUTION_ENABLED", "false")
    monkeypatch.setenv("CONTROLLED_STORY_EXECUTION_ENABLED", "false")
    monkeypatch.setenv("SCHEDULER_STORY_EXECUTION_ENABLED", "false")
    monkeypatch.setenv("STORY_EXECUTION_MODE", "live")
    monkeypatch.setenv("STORY_ACCOUNT_MUTATION_ALLOWLIST", "1")
    reset_provider_call_counter()
    decision = StoryMutationService.evaluate(
        account_id=1,
        trigger=StoryMutationTrigger.OPERATOR,
        scope="controlled_live",
        caller="incomplete_flags",
        skip_legacy_purpose_check=False,
    )
    assert decision.allowed is False
    assert decision.denial_reason == "controlled_story_execution_disabled"
    assert get_provider_call_count() == 0


def test_production_mutations_flag_off_cannot_publish(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("STORY_MUTATIONS_ENABLED", "false")
    monkeypatch.setenv("CONTROLLED_STORY_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("STORY_EXECUTION_MODE", "live")
    monkeypatch.setenv("STORY_ACCOUNT_MUTATION_ALLOWLIST", "1")
    decision = _evaluate()
    assert decision.allowed is False
    assert decision.denial_reason == "story_mutations_disabled"
    assert get_provider_call_count() == 0


_SECRET_MARKERS = (
    "TELEGRAM_API_HASH",
    "TELEGRAM_SESSION_ENCRYPTION_KEY_B64",
    "BOT_TOKEN",
    "OPENAI_API_KEY",
    "META_APP_SECRET",
    "DASHBOARD_SECRET_KEY",
    "DASHBOARD_ADMIN_TOKEN",
    "session_string",
    "api_hash",
)


def test_debug_unavailable_in_production(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("DASHBOARD_ADMIN_TOKEN", "pytest-debug-token")
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "pytest-not-a-real-secret")
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    rv = app.test_client().get("/debug")
    assert rv.status_code == 404
    body = rv.get_data(as_text=True)
    for marker in _SECRET_MARKERS:
        assert marker not in body


def test_debug_local_has_no_secrets(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("DASHBOARD_ADMIN_TOKEN", "pytest-debug-token")
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "pytest-not-a-real-secret")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("STORY_MUTATIONS_ENABLED", "true")
    from src.dashboard.app import create_app, build_debug_runtime_payload

    payload = build_debug_runtime_payload()
    assert payload["environment"] == "development"
    assert payload["story_mutations_enabled"] is False
    blob = json.dumps(payload)
    for marker in _SECRET_MARKERS:
        assert marker not in blob

    app = create_app()
    app.config["TESTING"] = True
    rv = app.test_client().get("/debug")
    assert rv.status_code == 200
    data = rv.get_json()
    assert set(data) >= {
        "environment",
        "database",
        "campaign_execution_enabled",
        "controlled_story_execution_enabled",
        "scheduler_story_execution_enabled",
        "story_mutations_enabled",
        "mentions_certified",
    }
    assert data["story_mutations_enabled"] is False
    assert "TELEGRAM_API_HASH" not in json.dumps(data)
