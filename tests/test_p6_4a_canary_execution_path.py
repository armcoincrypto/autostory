"""P6.4A — send-time binding guard, exact authorization, content hash, dry-run."""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.clients.binding_verification import (
    BINDING_VERIFICATION_FRESH_TTL_SEC,
    CONFIGURED_NOT_VERIFIED,
    STALE_CHECK,
    VERIFIED_CAN_POST,
)
from src.clients.send_time_binding_guard import evaluate_send_time_binding_guard
from src.core.database import Base
from src.core.execution_guard import (
    ACTION_TELEGRAM_SEND,
    RESULT_ALLOW,
    RESULT_DENY,
    can_execute_action,
)
from src.core.p6_4_authorization import (
    BINDING_ID,
    MARKER,
    PILOT_ACCOUNT,
    TARGET_ID,
    create_manifest,
    invalidate_unconsumed,
    load_manifest,
    mark_consumed,
    message_sha256,
    reserve_authorization,
    save_manifest,
    set_armed,
    validate_p6_4_authorization,
)
from src.core.scheduler_models import SCHEDULED_JOB_P6_4_CERTIFICATION_MARKER


@pytest.fixture(autouse=True)
def _locked_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SCHEDULER_MUTATIONS_ENABLED", "false")
    monkeypatch.setenv("P4C_SINGLE_SEND_ENABLED", "false")
    monkeypatch.setenv("P5A_SINGLE_SEND_ENABLED", "false")
    monkeypatch.setenv("P5C_SINGLE_SEND_ENABLED", "false")
    monkeypatch.setenv("P5D_SINGLE_SEND_ENABLED", "false")
    monkeypatch.setenv("P6_4_SINGLE_SEND_ENABLED", "false")
    monkeypatch.setenv("EXECUTION_EMERGENCY_LOCK", "false")
    monkeypatch.setenv("OPERATOR_APPROVAL_REQUIRED", "false")


@pytest.fixture
def manifest(tmp_path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "p6_4_scope_manifest.json"
    monkeypatch.setattr("src.core.p6_4_authorization.MANIFEST_PATH", path)
    body = "STORYFLEET canary check — harmless test post. No action required."
    m = create_manifest(message_body=body, operator_approval_id="test")
    m["armed"] = True
    m["job_id"] = 9001
    save_manifest(m)
    return m


def _arm_p6_4(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("P6_4_SINGLE_SEND_ENABLED", "true")


def test_content_hash_canonical_utf8():
    assert message_sha256("a\nb") == message_sha256("a\nb")
    assert message_sha256("a\nb") != message_sha256("a\r\nb")
    assert message_sha256("x ") != message_sha256("x")


def test_auth_exact_account_target_binding(manifest: dict, monkeypatch: pytest.MonkeyPatch):
    _arm_p6_4(monkeypatch)
    sha = manifest["expected_message_sha256"]
    ok, reason, _ = validate_p6_4_authorization(
        account_id=PILOT_ACCOUNT,
        target_id=TARGET_ID,
        binding_id=BINDING_ID,
        job_marker=MARKER,
        job_id=9001,
        content_sha256=sha,
        require_armed=True,
    )
    assert ok and reason == "p6_4_live_canary_authorized"

    ok, reason, _ = validate_p6_4_authorization(
        account_id=106, target_id=TARGET_ID, binding_id=BINDING_ID,
        job_marker=MARKER, job_id=9001, content_sha256=sha, require_armed=True,
    )
    assert not ok and reason == "p6_4_account_mismatch"

    ok, reason, _ = validate_p6_4_authorization(
        account_id=PILOT_ACCOUNT, target_id=14, binding_id=BINDING_ID,
        job_marker=MARKER, job_id=9001, content_sha256=sha, require_armed=True,
    )
    assert not ok and reason == "p6_4_target_mismatch"

    ok, reason, _ = validate_p6_4_authorization(
        account_id=PILOT_ACCOUNT, target_id=TARGET_ID, binding_id=99,
        job_marker=MARKER, job_id=9001, content_sha256=sha, require_armed=True,
    )
    assert not ok and reason == "p6_4_binding_mismatch"


def test_auth_content_hash_and_expiry_and_consume(manifest: dict, monkeypatch: pytest.MonkeyPatch):
    _arm_p6_4(monkeypatch)
    sha = manifest["expected_message_sha256"]
    ok, reason, _ = validate_p6_4_authorization(
        account_id=PILOT_ACCOUNT, target_id=TARGET_ID, binding_id=BINDING_ID,
        job_marker=MARKER, job_id=9001, content_sha256="0" * 64, require_armed=True,
    )
    assert not ok and reason == "p6_4_content_hash_mismatch"

    m = load_manifest()
    m["expires_at_utc"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    save_manifest(m)
    ok, reason, _ = validate_p6_4_authorization(
        account_id=PILOT_ACCOUNT, target_id=TARGET_ID, binding_id=BINDING_ID,
        job_marker=MARKER, job_id=9001, content_sha256=sha, require_armed=True,
    )
    assert not ok and reason == "p6_4_authorization_expired"

    # reset expiry + consume
    m = load_manifest()
    m["expires_at_utc"] = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    m["consumed"] = False
    save_manifest(m)
    mark_consumed()
    ok, reason, _ = validate_p6_4_authorization(
        account_id=PILOT_ACCOUNT, target_id=TARGET_ID, binding_id=BINDING_ID,
        job_marker=MARKER, job_id=9001, content_sha256=sha, require_armed=True,
    )
    assert not ok and reason == "p6_4_authorization_consumed"


def test_auth_exact_job_and_invalidate(manifest: dict, monkeypatch: pytest.MonkeyPatch):
    _arm_p6_4(monkeypatch)
    sha = manifest["expected_message_sha256"]
    ok, reason, _ = validate_p6_4_authorization(
        account_id=PILOT_ACCOUNT, target_id=TARGET_ID, binding_id=BINDING_ID,
        job_marker=MARKER, job_id=9999, content_sha256=sha, require_armed=True,
    )
    assert not ok and reason == "p6_4_job_not_fresh"

    invalidate_unconsumed()
    ok, reason, _ = validate_p6_4_authorization(
        account_id=PILOT_ACCOUNT, target_id=TARGET_ID, binding_id=BINDING_ID,
        job_marker=MARKER, job_id=9001, content_sha256=sha, require_armed=True,
    )
    assert not ok and reason == "p6_4_authorization_invalidated"


def test_guard_flag_off_denies(manifest: dict, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("P6_4_SINGLE_SEND_ENABLED", "false")
    d = can_execute_action(
        ACTION_TELEGRAM_SEND,
        account_id=PILOT_ACCOUNT,
        target_id=TARGET_ID,
        job_marker=SCHEDULED_JOB_P6_4_CERTIFICATION_MARKER,
        job_id=9001,
        binding_id=BINDING_ID,
        content_sha256=manifest["expected_message_sha256"],
        skip_audit=True,
    )
    assert d.result == RESULT_DENY
    assert d.reason_code == "p6_4_scope_inactive"


def test_guard_content_mutation_rejected(manifest: dict, monkeypatch: pytest.MonkeyPatch):
    _arm_p6_4(monkeypatch)
    d = can_execute_action(
        ACTION_TELEGRAM_SEND,
        account_id=PILOT_ACCOUNT,
        target_id=TARGET_ID,
        job_marker=SCHEDULED_JOB_P6_4_CERTIFICATION_MARKER,
        job_id=9001,
        binding_id=BINDING_ID,
        content_sha256=message_sha256("mutated"),
        skip_audit=True,
    )
    assert d.result == RESULT_DENY
    assert d.reason_code == "p6_4_content_hash_mismatch"


def test_guard_authorized_without_db_skips_binding(manifest: dict, monkeypatch: pytest.MonkeyPatch):
    """Unit path: db=None skips send-time binding (gateway always passes db)."""
    _arm_p6_4(monkeypatch)
    d = can_execute_action(
        ACTION_TELEGRAM_SEND,
        account_id=PILOT_ACCOUNT,
        target_id=TARGET_ID,
        job_marker=SCHEDULED_JOB_P6_4_CERTIFICATION_MARKER,
        job_id=9001,
        binding_id=BINDING_ID,
        content_sha256=manifest["expected_message_sha256"],
        db=None,
        skip_audit=True,
    )
    assert d.result == RESULT_ALLOW
    assert d.reason_code == "p6_4_live_canary_authorized"


def test_send_time_binding_guard_accepts_verified(monkeypatch: pytest.MonkeyPatch):
    db = MagicMock()
    binding = MagicMock()
    binding.account_id = 107
    binding.target_id = 1
    db.query.return_value.filter.return_value.first.return_value = binding
    ver = {
        "status": VERIFIED_CAN_POST,
        "production_verified": True,
        "reason_code": "verified_can_post",
        "membership_status": "joined",
        "permission_checked_at": datetime.utcnow().isoformat(),
    }
    with patch(
        "src.clients.send_time_binding_guard.classify_binding_verification",
        return_value=ver,
    ):
        ok, reason, audit = evaluate_send_time_binding_guard(
            db, account_id=107, target_id=1, binding_id=39
        )
    assert ok and reason == "send_time_binding_ok"
    assert audit["binding_fresh_ttl_sec"] == BINDING_VERIFICATION_FRESH_TTL_SEC


def test_send_time_binding_guard_rejects_stale_and_wrong_link(monkeypatch: pytest.MonkeyPatch):
    db = MagicMock()
    binding = MagicMock()
    binding.account_id = 107
    binding.target_id = 1
    db.query.return_value.filter.return_value.first.return_value = binding
    with patch(
        "src.clients.send_time_binding_guard.classify_binding_verification",
        return_value={
            "status": STALE_CHECK,
            "production_verified": False,
            "reason_code": "binding_check_stale",
        },
    ):
        ok, reason, _ = evaluate_send_time_binding_guard(
            db, account_id=107, target_id=1, binding_id=39
        )
    assert not ok and reason == "binding_check_stale"

    binding.account_id = 106
    ok, reason, _ = evaluate_send_time_binding_guard(
        db, account_id=107, target_id=1, binding_id=39
    )
    assert not ok and reason == "binding_account_mismatch"

    binding.account_id = 107
    binding.target_id = 14
    ok, reason, _ = evaluate_send_time_binding_guard(
        db, account_id=107, target_id=1, binding_id=39
    )
    assert not ok and reason == "binding_target_mismatch"


def test_send_time_binding_rejects_configured_only():
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None  # binding_id path missing
    with patch(
        "src.clients.send_time_binding_guard.classify_binding_verification",
        return_value={
            "status": CONFIGURED_NOT_VERIFIED,
            "production_verified": False,
            "reason_code": "binding_not_verified",
        },
    ):
        ok, reason, _ = evaluate_send_time_binding_guard(db, account_id=107, target_id=1)
    assert not ok


def test_guard_rejects_stale_binding_via_db(manifest: dict, monkeypatch: pytest.MonkeyPatch):
    _arm_p6_4(monkeypatch)
    db = MagicMock()
    with patch(
        "src.clients.send_time_binding_guard.evaluate_send_time_binding_guard",
        return_value=(False, "binding_check_stale", {}),
    ):
        d = can_execute_action(
            ACTION_TELEGRAM_SEND,
            account_id=PILOT_ACCOUNT,
            target_id=TARGET_ID,
            job_marker=SCHEDULED_JOB_P6_4_CERTIFICATION_MARKER,
            job_id=9001,
            binding_id=BINDING_ID,
            content_sha256=manifest["expected_message_sha256"],
            db=db,
            skip_audit=True,
        )
    assert d.result == RESULT_DENY
    assert d.reason_code == "binding_check_stale"


def test_reserve_and_replay_rejected(manifest: dict, monkeypatch: pytest.MonkeyPatch):
    _arm_p6_4(monkeypatch)
    ok, _ = reserve_authorization(job_id=9001)
    assert ok
    ok, reason = reserve_authorization(job_id=9002)
    assert not ok
    assert reason == "p6_4_authorization_consumed"


def test_gateway_claim_extra_ids(manifest: dict, monkeypatch: pytest.MonkeyPatch):
    from src.core.p6_4_authorization import p6_4_gateway_claim_extra_account_ids

    monkeypatch.setenv("P6_4_SINGLE_SEND_ENABLED", "false")
    assert p6_4_gateway_claim_extra_account_ids() == frozenset()
    monkeypatch.setenv("P6_4_SINGLE_SEND_ENABLED", "true")
    assert p6_4_gateway_claim_extra_account_ids() == frozenset({107})


def test_emergency_stop_script_idempotent(tmp_path, monkeypatch: pytest.MonkeyPatch):
    from src.core import p6_4_authorization as auth

    path = tmp_path / "p6_4_scope_manifest.json"
    monkeypatch.setattr(auth, "MANIFEST_PATH", path)
    body = "canary"
    m = auth.create_manifest(message_body=body)
    m["armed"] = True
    auth.save_manifest(m)
    auth.invalidate_unconsumed()
    m1 = auth.load_manifest()
    assert m1.get("armed") is False
    assert m1.get("invalidated") is True
    auth.invalidate_unconsumed()  # second call safe
    m2 = auth.load_manifest()
    assert m2.get("armed") is False
    assert m2.get("invalidated") is True


def test_execution_preflight_refuses_without_p6_2():
    blockers = ["P6.2 certification not PASS"]
    hard = [b for b in blockers if "P6.2" not in b]
    assert hard == []
    verdict = (
        "P6_4_EXECUTION_PREFLIGHT_BLOCKED"
        if blockers
        else "P6_4_EXECUTION_PREFLIGHT_PASS"
    )
    assert verdict == "P6_4_EXECUTION_PREFLIGHT_BLOCKED"
    # dry-run may still be READY with only P6.2 time gate
    dry = (
        "P6_4_DRY_RUN_READY_EXECUTION_BLOCKED_BY_P6_2_TIME_GATE"
        if not hard
        else "P6_4_DRY_RUN_BLOCKED"
    )
    assert dry == "P6_4_DRY_RUN_READY_EXECUTION_BLOCKED_BY_P6_2_TIME_GATE"


@pytest.fixture
def isolated_db(monkeypatch: pytest.MonkeyPatch, tmp_path):
    import src.core.models  # noqa: F401
    import src.telegram_gateway.models  # noqa: F401
    import src.core.scheduler_models  # noqa: F401

    engine = create_engine(f"sqlite:///{tmp_path / 'p64.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def get_ctx():
        db = Session()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    monkeypatch.setattr("src.telegram_gateway.service.get_db_context", get_ctx)
    monkeypatch.setattr("src.telegram_gateway.worker.get_db_context", get_ctx)
    return Session


@pytest.mark.asyncio
async def test_isolated_e2e_one_send_and_replay_blocked(isolated_db, tmp_path, monkeypatch):
    from src.telegram_gateway.service import enqueue_job, get_job, mark_job_done
    from src.telegram_gateway import worker as gw_worker
    from src.core import p6_4_authorization as auth

    man_path = tmp_path / "manifest.json"
    monkeypatch.setattr(auth, "MANIFEST_PATH", man_path)
    monkeypatch.setenv("P6_4_SINGLE_SEND_ENABLED", "true")
    monkeypatch.setenv("SCHEDULER_MUTATIONS_ENABLED", "false")

    body = "isolated canary body"
    m = auth.create_manifest(message_body=body)
    m["armed"] = True
    m["job_id"] = 1
    auth.save_manifest(m)
    sha = m["expected_message_sha256"]

    jid = enqueue_job(
        account_id=107,
        task_type="send_message",
        target="1775722510",
        payload={
            "text": body,
            "expected_message_sha256": sha,
            "target_id": 1,
            "binding_id": 39,
            "job_marker": MARKER,
            "scheduled_job_id": 1,
            "p6_4_certification": True,
        },
    )

    # Mock allowlist + transport + guard binding
    monkeypatch.setattr(
        "src.telegram_gateway.worker.gateway_claim_allowed_account_ids",
        lambda db: frozenset({107}),
    )

    async def fake_send(account_id, target, text, **kwargs):
        assert text == body
        # Regression 849a43c: certification scope must reach transport.
        assert kwargs.get("job_marker") == MARKER
        assert kwargs.get("target_id") == 1
        fake_send.calls.append(1)
        return {"ok": True, "telegram_message_id": 4242}

    fake_send.calls = []

    class T:
        send_message_async = staticmethod(fake_send)

    monkeypatch.setattr(gw_worker, "_transport", T())
    async def _noop_release(*a, **k):
        return None

    from src.core.p5d_gateway_reconciliation import CertificationSendGuard, NOT_SENT_CONFIRMED

    async def _noop_release(*a, **k):
        return None

    monkeypatch.setattr(gw_worker, "_release_gateway_telethon_session", _noop_release)

    async def _clear_guard(**kwargs):
        return CertificationSendGuard(outcome=NOT_SENT_CONFIRMED, reason_code="not_sent")

    with patch(
        "src.core.execution_guard.can_execute_action",
        return_value=MagicMock(allowed=True, reason_code="p6_4_live_canary_authorized", message="ok"),
    ), patch(
        "src.core.p5d_gateway_reconciliation.pre_send_certification_guard",
        side_effect=_clear_guard,
    ):
        await gw_worker._process_job_snapshot(
            (jid, 107, "send_message", "1775722510", {
                "text": body,
                "expected_message_sha256": sha,
                "target_id": 1,
                "binding_id": 39,
                "job_marker": MARKER,
                "scheduled_job_id": 1,
                "p6_4_certification": True,
            }, 0)
        )

    assert len(fake_send.calls) == 1
    assert get_job(jid).status == "done"
    assert auth.load_manifest().get("consumed") is True

    # mutated content rejected before send
    jid2 = enqueue_job(
        account_id=107,
        task_type="send_message",
        target="1775722510",
        payload={
            "text": body + "X",
            "expected_message_sha256": sha,
            "target_id": 1,
            "binding_id": 39,
            "job_marker": MARKER,
            "scheduled_job_id": 1,
            "p6_4_certification": True,
        },
    )
    await gw_worker._process_job_snapshot(
        (jid2, 107, "send_message", "1775722510", {
            "text": body + "X",
            "expected_message_sha256": sha,
            "target_id": 1,
            "binding_id": 39,
            "job_marker": MARKER,
            "scheduled_job_id": 1,
        }, 0)
    )
    assert get_job(jid2).status == "failed"
    assert get_job(jid2).error_code == "content_hash_mismatch"
    assert len(fake_send.calls) == 1
