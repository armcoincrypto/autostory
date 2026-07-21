#!/usr/bin/env python3
"""Archive and remove irrecoverable orphan rows on a disposable SQLite copy.

Dry-run is the default. Mutation requires all of:
``--apply``, ``--acknowledge-disposable-copy``, and an exact
``--expected-violations`` precondition. Use ``--protected-path`` to identify
live databases that must never be mutated.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ALLOWED_TABLES = {
    "account_risk_events",
    "account_target_membership_probes",
    "message_deliveries",
    "schedule_profiles",
    "schedule_rules",
    "scheduled_jobs",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("--expected-violations", type=int, required=True)
    parser.add_argument("--protected-path", type=Path, action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--acknowledge-disposable-copy", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def same_file(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve() or left.samefile(right)
    except (FileNotFoundError, OSError):
        return left.resolve() == right.resolve()


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def table_counts(db: sqlite3.Connection) -> dict[str, int]:
    names = [
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    return {
        name: int(db.execute(f"SELECT count(*) FROM {quote_identifier(name)}").fetchone()[0])
        for name in names
    }


def business_aggregates(db: sqlite3.Connection) -> dict[str, int]:
    checks = {
        "active_accounts": "SELECT count(*) FROM accounts WHERE status='active'",
        "sent_deliveries": "SELECT count(*) FROM message_deliveries WHERE status='SENT'",
        "pending_jobs": "SELECT count(*) FROM scheduled_jobs WHERE status='PENDING'",
        "running_jobs": "SELECT count(*) FROM scheduled_jobs WHERE status='RUNNING'",
        "published_stories": "SELECT count(*) FROM stories WHERE status='published'",
    }
    result: dict[str, int] = {}
    for name, sql in checks.items():
        try:
            result[name] = int(db.execute(sql).fetchone()[0])
        except sqlite3.Error:
            result[name] = -1
    return result


def row_as_json(db: sqlite3.Connection, table: str, rowid: int) -> str:
    db.row_factory = sqlite3.Row
    row = db.execute(
        f"SELECT * FROM {quote_identifier(table)} WHERE rowid=?", (rowid,)
    ).fetchone()
    if row is None:
        raise RuntimeError(f"missing violating row {table}:{rowid}")
    return json.dumps(dict(row), sort_keys=True, default=str)


def run(args: argparse.Namespace) -> dict[str, Any]:
    path = args.database.resolve()
    if not path.is_file():
        raise SystemExit(f"database not found: {path}")
    if any(same_file(path, protected) for protected in args.protected_path):
        raise SystemExit("refusing protected/live database target")
    if args.apply and not args.acknowledge_disposable_copy:
        raise SystemExit("--apply requires --acknowledge-disposable-copy")

    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.row_factory = sqlite3.Row
        before_violations = list(db.execute("PRAGMA foreign_key_check"))
        before_count = len(before_violations)
        if before_count != args.expected_violations:
            raise SystemExit(
                f"unexpected violation count: expected={args.expected_violations} actual={before_count}"
            )
        unexpected = sorted(
            {str(row["table"]) for row in before_violations} - ALLOWED_TABLES
        )
        if unexpected:
            raise SystemExit(f"unsupported violating tables: {','.join(unexpected)}")

        unique_rows = sorted(
            {(str(row["table"]), int(row["rowid"])) for row in before_violations}
        )
        rows_by_table = Counter(table for table, _ in unique_rows)
        before_tables = table_counts(db)
        before_business = business_aggregates(db)

        if not args.apply:
            return {
                "mode": "dry-run",
                "database": str(path),
                "foreign_key_violations_before": before_count,
                "violating_rows": len(unique_rows),
                "rows_by_table": dict(sorted(rows_by_table.items())),
                "would_archive_and_delete": len(unique_rows),
            }

        archived_at = datetime.now(timezone.utc).isoformat()
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS phase05_fk_orphan_archive (
                    archive_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_table TEXT NOT NULL,
                    source_rowid INTEGER NOT NULL,
                    row_json TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    archived_at TEXT NOT NULL,
                    UNIQUE(source_table, source_rowid)
                )
                """
            )
            for table, rowid in unique_rows:
                db.execute(
                    """
                    INSERT OR IGNORE INTO phase05_fk_orphan_archive
                    (source_table, source_rowid, row_json, reason, archived_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        table,
                        rowid,
                        row_as_json(db, table, rowid),
                        "orphaned_child_after_parent_delete",
                        archived_at,
                    ),
                )
            for table in sorted(rows_by_table):
                rowids = [rowid for name, rowid in unique_rows if name == table]
                placeholders = ",".join("?" for _ in rowids)
                db.execute(
                    f"DELETE FROM {quote_identifier(table)} "
                    f"WHERE rowid IN ({placeholders})",
                    rowids,
                )
            after_violations = list(db.execute("PRAGMA foreign_key_check"))
            if after_violations:
                raise RuntimeError(
                    f"repair left {len(after_violations)} foreign-key violations"
                )
            db.commit()
        except Exception:
            db.rollback()
            raise

        quick = db.execute("PRAGMA quick_check").fetchone()[0]
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        after_tables = table_counts(db)
        after_business = business_aggregates(db)
        deltas = {
            table: after_tables.get(table, 0) - before_tables.get(table, 0)
            for table in sorted(set(before_tables) | set(after_tables))
            if after_tables.get(table, 0) != before_tables.get(table, 0)
        }
        return {
            "mode": "apply",
            "database": str(path),
            "foreign_key_violations_before": before_count,
            "foreign_key_violations_after": 0,
            "archived_rows": len(unique_rows),
            "rows_by_table": dict(sorted(rows_by_table.items())),
            "table_row_count_deltas": deltas,
            "business_aggregates_before": before_business,
            "business_aggregates_after": after_business,
            "business_aggregates_stable": before_business == after_business,
            "quick_check": quick,
            "integrity_check": integrity,
        }


def main() -> int:
    args = parse_args()
    result = run(args)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(
            "mode={mode} violations_before={foreign_key_violations_before} "
            "rows={violating_rows}".format(
                violating_rows=result.get("violating_rows", result.get("archived_rows", 0)),
                **result,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
