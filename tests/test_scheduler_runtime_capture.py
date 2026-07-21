from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "audit" / "capture_scheduler_runtime.py"
SPEC = importlib.util.spec_from_file_location("capture_scheduler_runtime", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_safe_config_excludes_secret_values(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "READINESS_WORKER_ENABLED=false\n"
        "DASHBOARD_ADMIN_TOKEN=must-not-appear\n",
        encoding="utf-8",
    )

    captured = MODULE.safe_config(env)

    assert captured == {"READINESS_WORKER_ENABLED": "false"}
    assert "must-not-appear" not in repr(captured)


def test_stale_database_records_are_separate_from_process_truth(tmp_path: Path) -> None:
    database = tmp_path / "runtime.db"
    with sqlite3.connect(database) as db:
        db.executescript(
            """
            CREATE TABLE scheduled_jobs (
                id INTEGER PRIMARY KEY,
                status TEXT,
                updated_at TEXT
            );
            INSERT INTO scheduled_jobs VALUES (1, 'DONE', '2020-01-01T00:00:00Z');
            """
        )

    evidence = MODULE.database_evidence(database)

    assert evidence["scheduled_jobs_by_status"] == {"DONE": 1}
    assert evidence["scheduled_jobs_latest_updated_at"] == "2020-01-01T00:00:00Z"
    assert "currently_running" not in evidence


def test_service_inspection_does_not_execute_entrypoint(monkeypatch) -> None:
    calls = []

    class Result:
        returncode = 0
        stdout = "Id=example.service\nActiveState=active\n"

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return Result()

    monkeypatch.setattr(MODULE.subprocess, "run", fake_run)

    state = MODULE.service_state("example.service")

    assert state["ActiveState"] == "active"
    assert calls[0][0][:2] == ["systemctl", "show"]
    assert all(action not in calls[0][0] for action in ("start", "restart", "reload"))
