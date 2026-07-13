"""P6.2A soak checkpoint and certification tooling tests."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

REPO = Path("/opt/autostory")
CHECKPOINT = REPO / "scripts/ops/p6_2_readiness_worker_soak_checkpoint.py"
CERTIFY = REPO / "scripts/ops/p6_2_readiness_worker_soak_certify.py"


def test_certify_rejects_under_24h(tmp_path):
    baseline = {
        "soak_id": "test",
        "soak_start_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "account_107": {"status": "READY"},
        "account_139": {"status": "NOT_AUTHORIZED"},
    }
    bpath = tmp_path / "baseline.json"
    bpath.write_text(json.dumps(baseline), encoding="utf-8")
    cpdir = tmp_path / "cps"
    cpdir.mkdir()
    out = subprocess.run(
        [sys.executable, str(CERTIFY), "--baseline", str(bpath), "--checkpoints-dir", str(cpdir)],
        capture_output=True,
        text=True,
        cwd=str(REPO),
    )
    assert out.returncode == 2
    data = json.loads(out.stdout)
    assert data["verdict"] == "BLOCKED"
    assert any("86400" in b for b in data["blockers"])


def test_certify_accepts_24h_with_checkpoints(tmp_path):
    start = datetime.now(timezone.utc) - timedelta(hours=25)
    baseline = {
        "soak_id": "test",
        "soak_start_utc": start.isoformat().replace("+00:00", "Z"),
        "account_107": {"status": "READY"},
        "account_139": {"status": "NOT_AUTHORIZED"},
        "queue": {"pending_running": 0, "max_job_id": 366, "max_delivery_id": 150, "max_gateway_job_id": 6069, "delivery_count": 144},
        "protected_evidence_fingerprint": "abc",
    }
    bpath = tmp_path / "baseline.json"
    bpath.write_text(json.dumps(baseline), encoding="utf-8")
    cpdir = tmp_path / "cps"
    cpdir.mkdir()
    for i in range(50):
        ts = start + timedelta(minutes=15 * i)
        cp = {
            "checkpoint_utc": ts.isoformat().replace("+00:00", "Z"),
            "services": {"autostory-readiness-worker": {"active": "active"}},
            "evaluation": {"blockers": [], "warnings": []},
            "account_107": {"status": "READY"},
            "account_139": {"status": "NOT_AUTHORIZED"},
            "queue": baseline["queue"],
            "protected_evidence_fingerprint": "abc",
            "cycle_metrics": {"last_cycle_duration_sec": 12.0},
        }
        (cpdir / f"checkpoint_{i:04d}.json").write_text(json.dumps(cp), encoding="utf-8")
    out = subprocess.run(
        [sys.executable, str(CERTIFY), "--baseline", str(bpath), "--checkpoints-dir", str(cpdir)],
        capture_output=True,
        text=True,
        cwd=str(REPO),
    )
    assert out.returncode == 0
    data = json.loads(out.stdout)
    assert data["verdict"] == "PASS"
    assert data["elapsed_seconds"] >= 86400


def test_checkpoint_script_runs():
    out = subprocess.run(
        [sys.executable, str(CHECKPOINT)],
        capture_output=True,
        text=True,
        cwd=str(REPO),
        timeout=120,
    )
    # May exit 1 or 2 if gateway active etc — must produce JSON
    assert "checkpoint" in out.stdout or "evaluation" in out.stdout
