"""P5C scoped second-target pilot guard tests."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from config.settings import settings
from src.core.execution_guard import (
    ACTION_TELEGRAM_SEND,
    RESULT_ALLOW,
    RESULT_DENY,
    can_execute_action,
)
from src.core.p5c_authorization import (
    MARKER,
    create_manifest,
    load_manifest,
    save_manifest,
    validate_p5c_authorization,
)
from src.core.p5c_send_counter import p5c_live_send_count, p5c_live_send_remaining
from src.core.scheduler_models import SCHEDULED_JOB_P5C_CERTIFICATION_MARKER


@pytest.fixture(autouse=True)
def _locked_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SCHEDULER_MUTATIONS_ENABLED", "false")
    monkeypatch.setenv("P4C_SINGLE_SEND_ENABLED", "false")
    monkeypatch.setenv("P5A_SINGLE_SEND_ENABLED", "false")
    monkeypatch.setenv("P5C_SINGLE_SEND_ENABLED", "false")
    monkeypatch.setenv("SCHEDULER_MUTATION_SCOPE", "")
    monkeypatch.setenv("SCHEDULER_MUTATION_ACCOUNT_ALLOWLIST", "")


@pytest.fixture
def manifest(tmp_path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "p5c_scope_manifest.json"
    counter = tmp_path / "p5c_counter.json"
    monkeypatch.setattr("src.core.p5c_authorization.MANIFEST_PATH", path)
    monkeypatch.setattr("src.core.p5c_send_counter.STATE_PATH", counter)
    body = "P5C controlled second-target connectivity test 20260712T120000Z. No action required."
    m = create_manifest(message_body=body, operator_approved=True)
    m["armed"] = True
    save_manifest(m)
    return m


def _scoped_armed(monkeypatch: pytest.MonkeyPatch, *, p5c_enabled: bool = True):
    monkeypatch.setenv("SCHEDULER_MUTATION_SCOPE", "send_test_only")
    monkeypatch.setenv("SCHEDULER_MUTATION_ACCOUNT_ALLOWLIST", "107")
    monkeypatch.setenv("P5C_SINGLE_SEND_ENABLED", "true" if p5c_enabled else "false")
    settings.scheduler_mutations_enabled = False
    settings.scheduler_mutation_scope = "send_test_only"
    settings.scheduler_mutation_account_allowlist = "107"
    settings.p5c_single_send_enabled = p5c_enabled


def test_p5c_scope_disabled_deny(monkeypatch: pytest.MonkeyPatch, manifest: dict):
    _scoped_armed(monkeypatch, p5c_enabled=False)
    d = can_execute_action(
        ACTION_TELEGRAM_SEND,
        account_id=107,
        target_id=14,
        job_marker=SCHEDULED_JOB_P5C_CERTIFICATION_MARKER,
        job_id=400,
        skip_audit=True,
    )
    assert d.result == RESULT_DENY
    assert d.reason_code == "p5c_scope_inactive"


def test_p5c_expired_authorization_deny(monkeypatch: pytest.MonkeyPatch, manifest: dict):
    _scoped_armed(monkeypatch)
    m = load_manifest()
    m["expires_at_utc"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    save_manifest(m)
    ok, reason, _ = validate_p5c_authorization(
        account_id=107, target_id=14, job_marker=MARKER, require_armed=True
    )
    assert not ok
    assert reason == "p5c_authorization_expired"


def test_p5c_wrong_target_deny(monkeypatch: pytest.MonkeyPatch, manifest: dict):
    _scoped_armed(monkeypatch)
    d = can_execute_action(
        ACTION_TELEGRAM_SEND,
        account_id=107,
        target_id=999,
        job_marker=SCHEDULED_JOB_P5C_CERTIFICATION_MARKER,
        skip_audit=True,
    )
    assert d.result == RESULT_DENY
    assert d.reason_code == "p5c_target_mismatch"


def test_p5c_valid_scope_allow(monkeypatch: pytest.MonkeyPatch, manifest: dict, tmp_path):
    counter = tmp_path / "p5c_counter.json"
    counter.write_text(json.dumps({"live_sends": 0}) + "\n")
    monkeypatch.setattr("src.core.p5c_send_counter.STATE_PATH", counter)
    _scoped_armed(monkeypatch)
    m = load_manifest()
    m["job_id"] = 402
    save_manifest(m)
    d = can_execute_action(
        ACTION_TELEGRAM_SEND,
        account_id=107,
        target_id=14,
        job_marker=SCHEDULED_JOB_P5C_CERTIFICATION_MARKER,
        job_id=402,
        skip_audit=True,
    )
    assert d.result == RESULT_ALLOW
    assert d.reason_code == "p5c_scoped_second_target_authorized"


def test_p5c_counter_at_limit_deny(monkeypatch: pytest.MonkeyPatch, manifest: dict, tmp_path):
    counter = tmp_path / "p5c_counter.json"
    counter.write_text(json.dumps({"live_sends": 1}) + "\n")
    monkeypatch.setattr("src.core.p5c_send_counter.STATE_PATH", counter)
    _scoped_armed(monkeypatch)
    d = can_execute_action(
        ACTION_TELEGRAM_SEND,
        account_id=107,
        target_id=14,
        job_marker=SCHEDULED_JOB_P5C_CERTIFICATION_MARKER,
        job_id=402,
        skip_audit=True,
    )
    assert d.result == RESULT_DENY
    assert d.reason_code == "p5c_counter_limit_reached"


def test_p5c_gateway_claim_extra_account_ids_when_armed(monkeypatch: pytest.MonkeyPatch, manifest: dict):
    from src.core.p5c_authorization import p5c_gateway_claim_extra_account_ids

    monkeypatch.setenv("P5C_SINGLE_SEND_ENABLED", "false")
    assert p5c_gateway_claim_extra_account_ids() == frozenset()
    monkeypatch.setenv("P5C_SINGLE_SEND_ENABLED", "true")
    assert p5c_gateway_claim_extra_account_ids() == frozenset({107})
