"""Wave 8 — automatic fleet matrix refresh contracts."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.stories.fleet_readiness_matrix import write_latest_matrix
from src.stories import refresh_fleet_matrix as rfm

ROOT = Path(__file__).resolve().parents[1]


def test_write_latest_matrix_is_atomic(tmp_path: Path):
    target = tmp_path / "latest.json"
    target.write_text('{"generated_at":"OLD"}\n', encoding="utf-8")
    matrix = {"generated_at": "NEW", "accounts": [], "totals": {"total_configured_accounts": 0}}
    write_latest_matrix(matrix, target)
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["generated_at"] == "NEW"
    # No leftover temp files
    temps = list(tmp_path.glob(".latest.json.*.tmp"))
    assert temps == []


def test_write_latest_matrix_failure_preserves_previous(tmp_path: Path, monkeypatch):
    target = tmp_path / "latest.json"
    target.write_text('{"generated_at":"KEEP"}\n', encoding="utf-8")

    real_replace = os.replace

    def boom(src, dst):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        write_latest_matrix({"generated_at": "BAD"}, target)
    assert json.loads(target.read_text())["generated_at"] == "KEEP"
    monkeypatch.setattr(os, "replace", real_replace)


def test_overlap_lock_skips_second_invocation(tmp_path: Path):
    lock = tmp_path / "refresh.lock"
    status = tmp_path / "status.json"
    latest = tmp_path / "latest.json"
    latest.write_text('{"generated_at":"BEFORE"}\n', encoding="utf-8")

    # Hold the lock in another thread.
    import fcntl

    holder = open(lock, "a+", encoding="utf-8")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    out = rfm.refresh_fleet_matrix(
        output_root=tmp_path / "audit",
        latest_path=latest,
        backup_dir=tmp_path / "backups",
        status_path=status,
        lock_path=lock,
        timeout_seconds=15.0,
    )
    assert out["skipped"] is True
    assert out["error"] == "LOCK_BUSY"
    assert json.loads(latest.read_text())["generated_at"] == "BEFORE"
    fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
    holder.close()


def test_probe_failure_preserves_previous_matrix(tmp_path: Path):
    latest = tmp_path / "latest.json"
    latest.write_text(
        json.dumps({"generated_at": "KEEP_ME", "accounts": [{"account_id": 1}]}) + "\n",
        encoding="utf-8",
    )

    import src.stories.fleet_certification as fc

    async def _boom(*a, **k):
        raise RuntimeError("probe exploded")

    with patch.object(fc, "audit_fleet", _boom):
        out = rfm.refresh_fleet_matrix(
            output_root=tmp_path / "audit",
            latest_path=latest,
            backup_dir=tmp_path / "backups",
            status_path=tmp_path / "status.json",
            lock_path=tmp_path / "lock",
        )
    assert out["ok"] is False
    assert "probe exploded" in (out.get("error") or "")
    assert json.loads(latest.read_text())["generated_at"] == "KEEP_ME"


def test_successful_refresh_updates_matrix(tmp_path: Path):
    latest = tmp_path / "latest.json"
    latest.write_text('{"generated_at":"OLD","accounts":[]}\n', encoding="utf-8")
    audit_report = {
        "audit_run_id": "fleet-certification-TEST",
        "audit_completed_at": "2026-09-08T00:00:00Z",
        "accounts": [],
        "production_mutations": {
            "stories_published": 0,
            "messages_sent": 0,
            "logins_attempted": 0,
            "account_state_changes": 0,
            "shared_env_modified": False,
            "source_session_files_modified": False,
        },
    }

    async def fake_audit(*_a, **_k):
        return audit_report

    def fake_write_artifacts(report, output_root: Path):
        evidence = Path(output_root) / report["audit_run_id"]
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / "fleet-readiness.json").write_text(
            json.dumps(report) + "\n", encoding="utf-8"
        )
        return evidence

    fake_matrix = {
        "generated_at": "2026-09-08T00:01:00Z",
        "accounts": [{"account_id": i} for i in range(3)],
        "totals": {
            "total_configured_accounts": 3,
            "classification_histogram": {
                "CERTIFIED_PUBLISH": 2,
                "AUTH_FAILED": 1,
                "ACCOUNT_DISABLED": 0,
                "INTENTIONALLY_EXCLUDED": 0,
            },
        },
    }

    import src.stories.fleet_certification as fc
    import src.stories.fleet_readiness_matrix as frm

    with patch.object(fc, "audit_fleet", fake_audit), patch.object(
        fc, "write_artifacts", fake_write_artifacts
    ), patch.object(frm, "build_canonical_matrix", return_value=dict(fake_matrix)):
        out = rfm.refresh_fleet_matrix(
            output_root=tmp_path / "audit",
            latest_path=latest,
            backup_dir=tmp_path / "backups",
            status_path=tmp_path / "status.json",
            lock_path=tmp_path / "lock",
        )
    assert out["ok"] is True
    written = json.loads(latest.read_text())
    assert written["generated_at"] == "2026-09-08T00:01:00Z"
    assert written["regeneration_method"] == rfm.REGENERATION_METHOD
    assert out["counts"]["certified"] == 2
    assert out["counts"]["auth_failed"] == 1
    # Backup of previous created
    backups = list((tmp_path / "backups").glob("latest.json.pre-auto-refresh-*"))
    assert len(backups) == 1


def test_fail_closed_env_applied():
    rfm._apply_fail_closed_env()
    assert os.environ.get("STORY_MUTATIONS_ENABLED") == "false"
    assert os.environ.get("MESSAGES_EXECUTION_ENABLED") == "false"
    assert os.environ.get("SCHEDULER_MUTATIONS_ENABLED") == "false"
    assert os.environ.get("MESSAGES_AI_DRAFT_ENABLED") == "false"


def test_systemd_unit_files_present_and_safe():
    service = (ROOT / "deploy/autostory-fleet-matrix-refresh.service").read_text()
    timer = (ROOT / "deploy/autostory-fleet-matrix-refresh.timer").read_text()
    assert "Type=oneshot" in service
    assert "WorkingDirectory=/opt/autostory-releases/current" in service
    assert "python -m src.stories.refresh_fleet_matrix" in service
    assert "MESSAGES_EXECUTION_ENABLED=false" in service
    assert "STORY_MUTATIONS_ENABLED=false" in service
    assert "SCHEDULER_MUTATIONS_ENABLED=false" in service
    assert "OnUnitActiveSec=6h" in timer
    assert "Persistent=true" in timer
    assert "OnBootSec=" in timer
    # Must not enable story/message sends
    assert "STORY_EXECUTION_ENABLED=true" not in service
    assert "MESSAGES_EXECUTION_ENABLED=true" not in service


def test_refresh_module_has_no_send_paths():
    text = (ROOT / "src/stories/refresh_fleet_matrix.py").read_text()
    assert "send_now" not in text
    assert "SendStoryRequest" not in text
    assert "OwnerDirectMessageService" not in text
    assert "MESSAGES_EXECUTION_ENABLED" in text  # fail-closed only


def test_messages_and_draft_untouched_by_wave8_diff_surface():
    """Guard: Wave 8 must not rewrite Messages / OpenAI draft modules."""
    # These files must still exist from Wave 7E-OAI and not be deleted by Wave 8.
    assert (ROOT / "src/messaging/message_draft_service.py").is_file()
    assert (ROOT / "src/messaging/openai_draft_provider.py").is_file()
    assert (ROOT / "src/messaging/owner_dm_service.py").is_file()
