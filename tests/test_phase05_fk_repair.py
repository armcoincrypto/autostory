from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]
CLASSIFIER = ROOT / "scripts" / "audit" / "classify_sqlite_fk_violations.py"
REPAIR = ROOT / "scripts" / "db" / "rehearse_fk_repair.py"


def create_fixture(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            PRAGMA foreign_keys=OFF;
            CREATE TABLE accounts (id INTEGER PRIMARY KEY, status TEXT);
            CREATE TABLE chat_targets (id INTEGER PRIMARY KEY);
            CREATE TABLE stories (id INTEGER PRIMARY KEY, status TEXT);
            CREATE TABLE account_risk_events (
                id INTEGER PRIMARY KEY,
                account_id INTEGER NOT NULL REFERENCES accounts(id),
                event_type TEXT
            );
            INSERT INTO accounts VALUES (1, 'active');
            INSERT INTO account_risk_events VALUES (1, 1, 'ok');
            INSERT INTO account_risk_events VALUES (2, 99, 'orphan');
            """
        )


def run_script(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def test_classifier_reconciles_exact_total_and_redacts_identifiers(tmp_path: Path) -> None:
    database = tmp_path / "fixture.db"
    create_fixture(database)

    result = run_script(CLASSIFIER, str(database), "--json")

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["foreign_key_violation_count"] == 1
    assert payload["classified_count"] == 1
    assert payload["violations"][0]["child_value"].startswith("sha256:")
    assert payload["root_cause_counts"] == {
        "ORPHANED_CHILD_AFTER_PARENT_DELETE": 1
    }


def test_repair_dry_run_does_not_mutate(tmp_path: Path) -> None:
    database = tmp_path / "fixture.db"
    create_fixture(database)

    result = run_script(
        REPAIR, str(database), "--expected-violations", "1", "--json"
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["mode"] == "dry-run"
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM pragma_foreign_key_check").fetchone()[0] == 1


def test_repair_refuses_protected_database(tmp_path: Path) -> None:
    database = tmp_path / "fixture.db"
    create_fixture(database)

    result = run_script(
        REPAIR,
        str(database),
        "--expected-violations",
        "1",
        "--protected-path",
        str(database),
        "--apply",
        "--acknowledge-disposable-copy",
    )

    assert result.returncode != 0
    assert "refusing protected/live database target" in result.stderr


def test_repair_archives_orphan_is_idempotent_and_preserves_aggregates(
    tmp_path: Path,
) -> None:
    database = tmp_path / "fixture.db"
    create_fixture(database)

    first = run_script(
        REPAIR,
        str(database),
        "--expected-violations",
        "1",
        "--apply",
        "--acknowledge-disposable-copy",
        "--json",
    )
    second = run_script(
        REPAIR,
        str(database),
        "--expected-violations",
        "0",
        "--apply",
        "--acknowledge-disposable-copy",
        "--json",
    )

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    payload = json.loads(first.stdout)
    assert payload["foreign_key_violations_after"] == 0
    assert payload["archived_rows"] == 1
    assert payload["business_aggregates_stable"] is True
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM account_risk_events").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM phase05_fk_orphan_archive").fetchone()[0] == 1


def test_repair_aborts_on_unexpected_count(tmp_path: Path) -> None:
    database = tmp_path / "fixture.db"
    create_fixture(database)

    result = run_script(
        REPAIR,
        str(database),
        "--expected-violations",
        "2",
        "--apply",
        "--acknowledge-disposable-copy",
    )

    assert result.returncode != 0
    assert "unexpected violation count" in result.stderr
