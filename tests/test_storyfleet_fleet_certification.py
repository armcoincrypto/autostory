"""Focused safety/classification tests for the read-only fleet audit."""
from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.stories.fleet_certification import (
    CLASSIFICATIONS,
    FORBIDDEN_TELEGRAM_OPERATIONS,
    ReadOnlyStoryClient,
    SessionInspection,
    calculate_totals,
    classify_account,
    mask_phone,
    probe_account_isolated,
    probe_account_telegram,
    render_markdown,
    sanitize_error,
    safety_snapshot,
    write_artifacts,
)


def account(**overrides):
    values = {
        "id": 10,
        "status": SimpleNamespace(value="active"),
        "purpose": "both",
        "health_status": None,
        "last_active": None,
        "last_story_success_at": None,
        "story_precheck_status": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def inspection(*, present=True, readable=True, error=None):
    return SessionInspection(
        kind="file",
        path=Path("/tmp/account_10.session"),
        present=present,
        readable=readable,
        error_code=error,
        diagnostics=[],
        source_sha256=None,
    )


def probe(**overrides):
    values = {
        "probe_status": "ok",
        "auth_valid": True,
        "identity_matches": True,
        "story_api_available": True,
        "story_probe_status": "allowed",
    }
    values.update(overrides)
    return values


def classify(
    *,
    acc=None,
    enabled=True,
    complete=True,
    session=None,
    telegram=None,
    certified=False,
):
    return classify_account(
        account=acc or account(),
        enabled=enabled,
        config_complete=complete,
        inspection=session or inspection(),
        probe=telegram,
        certified=certified,
    )


def test_classification_precedence_disabled_first():
    result, _ = classify(
        enabled=False,
        complete=False,
        session=inspection(present=False, readable=False),
        telegram=probe(probe_status="banned"),
    )
    assert result == "ACCOUNT_DISABLED"


def test_missing_configuration_and_session():
    result, reasons = classify(
        complete=False,
        session=inspection(present=False, readable=False),
        telegram=None,
    )
    assert result == "CONFIG_INCOMPLETE"
    assert "session_missing" in reasons


def test_corrupt_session():
    result, reasons = classify(
        session=inspection(readable=False, error="session_unreadable"),
        telegram=None,
    )
    assert result == "SESSION_CORRUPT"
    assert reasons == ["session_unreadable"]


def test_unauthorized_session():
    result, reasons = classify(
        telegram=probe(
            probe_status="unauthorized",
            auth_valid=False,
            identity_matches=False,
            story_api_available=False,
        )
    )
    assert result == "AUTH_FAILED"
    assert reasons == ["telegram_unauthorized"]


def test_stale_authorization_requires_history():
    acc = account(last_active=object())
    result, reasons = classify(
        acc=acc,
        telegram=probe(
            probe_status="timeout",
            auth_valid=False,
            identity_matches=False,
            story_api_available=False,
        ),
    )
    assert result == "AUTH_STALE"
    assert "last_auth_too_old" in reasons


def test_identity_mismatch_is_auth_failed():
    result, reasons = classify(
        telegram=probe(identity_matches=False)
    )
    assert result == "AUTH_FAILED"
    assert reasons == ["identity_mismatch"]


def test_success_without_certification_is_auth_ok():
    result, reasons = classify(telegram=probe(), certified=False)
    assert result == "AUTH_OK"
    assert reasons == ["no_certified_story_evidence"]


def test_success_with_durable_certification():
    result, reasons = classify(telegram=probe(), certified=True)
    assert result == "CERTIFIED_PUBLISH"
    assert reasons == ["durable_controlled_story_evidence"]


def test_story_api_unavailable_is_not_auth_ok():
    result, reasons = classify(
        telegram=probe(
            probe_status="story_probe_failed",
            story_api_available=False,
            story_probe_status="failed_check",
        )
    )
    assert result == "AUTH_FAILED"
    assert reasons == ["story_probe_failed"]


def test_flood_wait_and_banned_classification():
    flooded, _ = classify(
        telegram=probe(
            probe_status="flood_wait",
            auth_valid=False,
            identity_matches=False,
            story_api_available=False,
        )
    )
    banned, _ = classify(
        telegram=probe(
            probe_status="banned",
            auth_valid=False,
            identity_matches=False,
            story_api_available=False,
        )
    )
    assert flooded == "FLOOD_WAIT"
    assert banned == "BANNED"


@pytest.mark.asyncio
async def test_read_only_request_facade_rejects_unknown_request():
    underlying = AsyncMock()
    facade = ReadOnlyStoryClient(underlying)

    class SendStoryRequest:
        pass

    with pytest.raises(RuntimeError, match="not in fleet audit allowlist"):
        await facade(SendStoryRequest())
    underlying.assert_not_called()


def test_secret_redaction_and_phone_masking():
    text = sanitize_error(
        "api_hash=supersecret phone=+15551234567 path=/opt/autostory/data/sessions/a.session"
    )
    assert "supersecret" not in text
    assert "15551234567" not in text
    assert "/opt/autostory" not in text
    assert mask_phone("+15551234567") == "+15***4567"


def test_fleet_totals_are_internally_consistent():
    rows = [
        {"classification": "CERTIFIED_PUBLISH", "auth_valid": True, "identity_matches": True, "story_api_available": True},
        {"classification": "AUTH_OK", "auth_valid": True, "identity_matches": True, "story_api_available": True},
        {"classification": "AUTH_FAILED", "auth_valid": False, "identity_matches": False, "story_api_available": False},
        {"classification": "AUTH_STALE", "auth_valid": False, "identity_matches": False, "story_api_available": False},
        {"classification": "SESSION_CORRUPT", "auth_valid": False, "identity_matches": False, "story_api_available": False},
        {"classification": "ACCOUNT_DISABLED", "auth_valid": None, "identity_matches": None, "story_api_available": None},
    ]
    totals = calculate_totals(rows)
    assert totals == {
        "total_accounts": 6,
        "healthy": 2,
        "need_auth": 2,
        "broken_sessions": 1,
        "disabled": 1,
        "story_capable": 2,
        "already_certified": 1,
        "ready_for_next_canary": 1,
    }


def test_markdown_rows_are_deterministic_when_input_sorted():
    report = {
        "audit_run_id": "x",
        "audit_started_at": "a",
        "audit_completed_at": "b",
        "totals": {
            "total_accounts": 2,
            "healthy": 0,
            "need_auth": 0,
            "broken_sessions": 0,
            "disabled": 2,
            "story_capable": 0,
            "already_certified": 0,
            "ready_for_next_canary": 0,
        },
        "accounts": [
            {
                "account_id": 2, "telegram_username": None, "telegram_user_id": None,
                "configured_enabled": False, "session_type": "empty", "session_readable": False,
                "auth_valid": None, "story_api_available": None, "last_auth_at": None,
                "last_story_at": None, "controlled_publish_certified": False,
                "classification": "ACCOUNT_DISABLED", "reason_codes": ["account_disabled"],
            },
            {
                "account_id": 10, "telegram_username": None, "telegram_user_id": None,
                "configured_enabled": False, "session_type": "empty", "session_readable": False,
                "auth_valid": None, "story_api_available": None, "last_auth_at": None,
                "last_story_at": None, "controlled_publish_certified": False,
                "classification": "ACCOUNT_DISABLED", "reason_codes": ["account_disabled"],
            },
        ],
    }
    rendered = render_markdown(report)
    assert rendered.index("| 2 |") < rendered.index("| 10 |")


def test_all_primary_classifications_are_exactly_supported():
    assert set(CLASSIFICATIONS) == {
        "CERTIFIED_PUBLISH",
        "AUTH_OK",
        "AUTH_STALE",
        "AUTH_FAILED",
        "SESSION_CORRUPT",
        "ACCOUNT_DISABLED",
        "FLOOD_WAIT",
        "BANNED",
        "CONFIG_INCOMPLETE",
    }


def test_audit_module_has_no_publisher_or_controlled_live_import():
    source_path = Path("src/stories/fleet_certification.py")
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    assert not any("publisher" in name for name in imported)
    assert not any("controlled_live_run" in name for name in imported)


def test_audit_module_has_no_forbidden_telegram_calls():
    tree = ast.parse(
        Path("src/stories/fleet_certification.py").read_text(encoding="utf-8")
    )
    called_names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            called_names.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            called_names.add(node.func.attr)
    assert called_names.isdisjoint(FORBIDDEN_TELEGRAM_OPERATIONS)


def test_audit_module_never_mutates_execution_environment():
    source = Path("src/stories/fleet_certification.py").read_text(encoding="utf-8")
    assert "os.environ[" not in source
    assert "os.environ.update" not in source
    assert "os.putenv" not in source
    assert FORBIDDEN_TELEGRAM_OPERATIONS == {
        "SendStoryRequest",
        "EditStoryRequest",
        "DeleteStoriesRequest",
        "send_message",
        "send_file",
        "upload_file",
    }


def test_safety_snapshot_refuses_enabled_flags(monkeypatch):
    monkeypatch.setenv("STORY_EXECUTION_ENABLED", "true")
    snapshot = safety_snapshot()
    assert snapshot["safe"] is False
    assert "STORY_EXECUTION_ENABLED_enabled" in snapshot["blockers"]


def test_artifact_writer_produces_stable_redacted_bundle(tmp_path):
    row = {
        "account_id": 10,
        "telegram_username": "audit_user",
        "telegram_user_id": 123,
        "configured_enabled": True,
        "session_type": "file",
        "session_readable": True,
        "auth_valid": True,
        "story_api_available": True,
        "last_auth_at": "2026-07-19T00:00:00Z",
        "last_story_at": None,
        "controlled_publish_certified": False,
        "classification": "AUTH_OK",
        "reason_codes": ["no_certified_story_evidence"],
        "safe_error_summary": None,
        "session_location_redacted": "canonical:account_10.session",
    }
    totals = {
        "total_accounts": 1,
        "healthy": 1,
        "need_auth": 0,
        "broken_sessions": 0,
        "disabled": 0,
        "story_capable": 1,
        "already_certified": 0,
        "ready_for_next_canary": 1,
    }
    report = {
        "audit_run_id": "fleet-certification-fixture",
        "audit_started_at": "2026-07-19T00:00:00Z",
        "audit_completed_at": "2026-07-19T00:00:01Z",
        "totals": totals,
        "accounts": [row],
        "commands": ["fixture read-only audit"],
        "safety_before": {"safe": True},
        "safety_after": {"safe": True},
        "production_mutations": {
            "stories_published": 0,
            "messages_sent": 0,
            "logins_attempted": 0,
            "account_state_changes": 0,
            "shared_env_modified": False,
            "source_session_files_modified": False,
        },
    }

    output = write_artifacts(report, tmp_path)

    assert sorted(path.name for path in output.iterdir()) == [
        "README.md",
        "commands.log",
        "fleet-readiness.json",
        "fleet-readiness.md",
        "safety-verification.md",
        "test-results.txt",
    ]
    encoded = (output / "fleet-readiness.json").read_text(encoding="utf-8")
    assert json.loads(encoded)["accounts"][0]["account_id"] == 10
    assert encoded.endswith("\n")
    assert "session_string" not in encoded
    assert "api_hash" not in encoded
    assert "/opt/autostory/data/sessions" not in encoded


@pytest.mark.asyncio
async def test_per_account_exception_isolation():
    async def explode(*args, **kwargs):
        raise RuntimeError("account-specific failure")

    result = await probe_account_isolated(
        account(),
        inspection(),
        timeout_seconds=1,
        probe_runner=explode,
    )
    assert result["probe_status"] == "error"
    assert "RuntimeError" in result["safe_error_summary"]


@pytest.mark.asyncio
async def test_client_disconnect_cleanup(monkeypatch):
    from src.stories import fleet_certification as module

    class FakeClient:
        instance = None

        def __init__(self, *args, **kwargs):
            self.disconnected = False
            FakeClient.instance = self

        async def connect(self):
            return None

        async def is_user_authorized(self):
            return True

        async def get_me(self):
            return SimpleNamespace(
                id=123,
                username="audit_user",
                phone="15551234567",
                first_name="Audit",
                last_name="User",
            )

        async def __call__(self, request):
            return True

        async def disconnect(self):
            self.disconnected = True

    monkeypatch.setattr(module, "TelegramClient", FakeClient)
    monkeypatch.setattr(module, "StringSession", lambda raw: object())
    monkeypatch.setattr(module.settings.telegram, "api_id", 1)
    monkeypatch.setattr(module.settings.telegram, "api_hash", "not-a-secret")

    acc = SimpleNamespace(
        id=10,
        session_string="fixture-session",
        proxy_config=None,
        user_id=123,
        username="audit_user",
        phone_number="+15551234567",
        first_name="Audit",
        last_name="User",
    )
    session = SessionInspection(
        kind="string",
        path=None,
        present=True,
        readable=True,
        error_code=None,
        diagnostics=[],
        source_sha256=None,
    )
    result = await probe_account_telegram(acc, session, timeout_seconds=1)
    assert result["auth_valid"] is True
    assert result["story_api_available"] is True
    assert FakeClient.instance.disconnected is True
