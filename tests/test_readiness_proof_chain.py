"""P9.25 proof-aware readiness qualification."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.readiness.readiness_proof_chain import (
    REASON_AUTH_OK_NOT_ENABLED,
    evaluate_auth_ok_not_enabled,
    find_latest_live_auth_proof,
    is_stale_v1_legacy_error,
)
from src.readiness.readiness_worker_v2 import (
    WorkerReadinessStatus,
    derive_readiness_status,
)

_PILOT = 106
_PROD_SHA = "56cc8d03354fc2a044afd3444e6b3e70c326e368adb43f757e0e4f3abcfe3d93"


@pytest.fixture
def lab_reports(tmp_path: Path) -> Path:
    root = tmp_path / "recovery_lab"
    reports = root / "reports"
    reports.mkdir(parents=True)
    return root


def _write_proof(reports: Path, name: str, payload: dict) -> None:
    (reports / name).write_text(json.dumps(payload) + "\n", encoding="utf-8")


def _fleet_meta_v7(**overrides) -> dict:
    base = {
        "found": True,
        "account_id": _PILOT,
        "tier": "fleet",
        "schema_version": 8,
        "resolver_code": None,
        "session_exists": True,
        "session_path": "/tmp/account_106.session",
        "readiness_status": "ERROR",
        "readiness_failure_code": "legacy_sqlite_session_format",
        "is_reserved": False,
        "is_controller": False,
        "warnings": [],
    }
    base.update(overrides)
    return base


def test_production_proof_qualifies(lab_reports: Path, tmp_path: Path, monkeypatch) -> None:
    prod = tmp_path / "account_106.session"
    prod.write_bytes(b"x" * 8)
    monkeypatch.setattr(
        "src.readiness.readiness_proof_chain.sha256_file",
        lambda p: _PROD_SHA if Path(p) == prod else "other",
    )

    reports = lab_reports / "reports"
    _write_proof(
        reports,
        "p9_24_live_auth_proof_20260517T180654Z.json",
        {
            "phase": "P9.24",
            "account_id": _PILOT,
            "proof_status": "AUTH_OK",
            "proof_source": "production",
            "production_unchanged": True,
            "production_sha_before": _PROD_SHA,
            "production_sha_after": _PROD_SHA,
        },
    )
    _write_proof(
        reports,
        "p9_24_install_manifest_20260517T180645Z.json",
        {
            "account_id": _PILOT,
            "install_status": "INSTALLED",
            "production_sha256_after": _PROD_SHA,
        },
    )

    meta = _fleet_meta_v7(session_path=str(prod))
    qual = evaluate_auth_ok_not_enabled(meta, lab_reports)
    assert qual is not None
    assert qual["reason"] == REASON_AUTH_OK_NOT_ENABLED

    monkeypatch.setattr(
        "src.readiness.readiness_worker_v2.evaluate_auth_ok_not_enabled",
        lambda m, lab_root=None: evaluate_auth_ok_not_enabled(m, lab_reports),
    )
    derived = derive_readiness_status(meta)
    assert derived.status == WorkerReadinessStatus.AUTH_OK_NOT_ENABLED
    assert derived.failure_code is None


def test_v8_schema_stays_legacy_without_proof() -> None:
    meta = {
        "found": True,
        "account_id": _PILOT,
        "tier": "fleet",
        "schema_version": 8,
        "resolver_code": "legacy_sqlite_session_format",
        "session_exists": True,
        "readiness_status": "ERROR",
        "readiness_failure_code": "legacy_sqlite_session_format",
        "warnings": [],
    }
    derived = derive_readiness_status(meta)
    assert derived.status == WorkerReadinessStatus.LEGACY_SCHEMA


def test_schema7_without_proof_unknown_not_ready(lab_reports: Path) -> None:
    meta = _fleet_meta_v7()
    qual = evaluate_auth_ok_not_enabled(meta, lab_reports)
    assert qual is None

    derived = derive_readiness_status(meta)
    assert derived.status == WorkerReadinessStatus.UNKNOWN
    assert derived.reason == "schema_v7_awaiting_live_auth_proof"
    assert derived.failure_code is None


def test_stale_v1_legacy_detector() -> None:
    assert is_stale_v1_legacy_error(
        _fleet_meta_v7(readiness_failure_code="legacy_sqlite_session_format")
    )
    # P9.43: v8 is compatible; v1 legacy_sqlite with schema v8 is stale v1 metadata.
    assert is_stale_v1_legacy_error(
        _fleet_meta_v7(schema_version=8, readiness_failure_code="legacy_sqlite_session_format")
    )


def test_converted_proof_requires_manifest(lab_reports: Path, tmp_path: Path, monkeypatch) -> None:
    prod = tmp_path / "account_106.session"
    prod.write_bytes(b"y")
    monkeypatch.setattr(
        "src.readiness.readiness_proof_chain.sha256_file",
        lambda p: _PROD_SHA,
    )
    reports = lab_reports / "reports"
    _write_proof(
        reports,
        "p9_23_live_auth_proof_20260517T175114Z.json",
        {
            "phase": "P9.23",
            "account_id": _PILOT,
            "proof_status": "AUTH_OK",
            "production_unchanged": True,
        },
    )
    meta = _fleet_meta_v7(session_path=str(prod))
    assert evaluate_auth_ok_not_enabled(meta, lab_reports) is None

    _write_proof(
        reports,
        "p9_24_install_manifest_20260517T180645Z.json",
        {
            "account_id": _PILOT,
            "install_status": "INSTALLED",
            "production_sha256_after": _PROD_SHA,
        },
    )
    assert evaluate_auth_ok_not_enabled(meta, lab_reports) is not None


def test_find_latest_prefers_production_proof(lab_reports: Path) -> None:
    reports = lab_reports / "reports"
    _write_proof(
        reports,
        "p9_23_live_auth_proof_20260517T175114Z.json",
        {
            "account_id": _PILOT,
            "proof_status": "AUTH_OK",
            "phase": "P9.23",
        },
    )
    _write_proof(
        reports,
        "p9_24_live_auth_proof_20260517T180654Z.json",
        {
            "account_id": _PILOT,
            "proof_status": "AUTH_OK",
            "phase": "P9.24",
            "proof_source": "production",
        },
    )
    proof = find_latest_live_auth_proof(_PILOT, lab_reports)
    assert proof is not None
    assert proof.get("phase") == "P9.24"
