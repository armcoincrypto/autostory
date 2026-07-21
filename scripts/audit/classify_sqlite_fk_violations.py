#!/usr/bin/env python3
"""Classify SQLite foreign-key violations without exposing row contents."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any


ROOT_CAUSE_BY_TABLE = {
    "account_risk_events": "ORPHANED_CHILD_AFTER_PARENT_DELETE",
    "schedule_profiles": "ORPHANED_CHILD_AFTER_PARENT_DELETE",
    "schedule_rules": "ORPHANED_CHILD_AFTER_PARENT_DELETE",
    "message_deliveries": "ORPHANED_CHILD_AFTER_PARENT_DELETE",
    "account_target_membership_probes": "ORPHANED_CHILD_AFTER_PARENT_DELETE",
    "scheduled_jobs": "ORPHANED_CHILD_AFTER_PARENT_DELETE",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--include-identifiers",
        action="store_true",
        help="Include raw internal identifiers; use only in restricted evidence storage",
    )
    return parser.parse_args()


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def identifier(value: Any, include: bool) -> Any:
    if value is None or include:
        return value
    return "sha256:" + hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def classify(path: Path, include_identifiers: bool) -> dict[str, Any]:
    uri = f"file:{path.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        db.row_factory = sqlite3.Row
        quick = db.execute("PRAGMA quick_check").fetchone()[0]
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        violations = list(db.execute("PRAGMA foreign_key_check"))
        rows: list[dict[str, Any]] = []
        family_counts: Counter[tuple[str, str, int, str, str]] = Counter()
        cause_counts: Counter[str] = Counter()
        for violation in violations:
            child_table = str(violation["table"])
            rowid = violation["rowid"]
            parent_table = str(violation["parent"])
            fk_index = int(violation["fkid"])
            fk_rows = list(
                db.execute(f"PRAGMA foreign_key_list({quote_identifier(child_table)})")
            )
            fk = next(row for row in fk_rows if int(row["id"]) == fk_index)
            child_column = str(fk["from"])
            parent_column = str(fk["to"] or "rowid")
            child = db.execute(
                f"SELECT {quote_identifier(child_column)} AS child_value "
                f"FROM {quote_identifier(child_table)} WHERE rowid = ?",
                (rowid,),
            ).fetchone()
            child_value = child["child_value"] if child is not None else None
            cause = ROOT_CAUSE_BY_TABLE.get(
                child_table, "UNKNOWN_REQUIRES_MANUAL_REVIEW"
            )
            family_counts[
                (child_table, parent_table, fk_index, child_column, cause)
            ] += 1
            cause_counts[cause] += 1
            rows.append(
                {
                    "child_table": child_table,
                    "child_rowid_or_primary_key": identifier(rowid, include_identifiers),
                    "parent_table": parent_table,
                    "foreign_key_index": fk_index,
                    "foreign_key_column": child_column,
                    "child_value": identifier(child_value, include_identifiers),
                    "expected_parent_key": parent_column,
                    "root_cause_family": cause,
                }
            )
        families = [
            {
                "child_table": key[0],
                "parent_table": key[1],
                "foreign_key_index": key[2],
                "foreign_key_column": key[3],
                "root_cause_family": key[4],
                "violation_count": count,
            }
            for key, count in sorted(family_counts.items())
        ]
        return {
            "database": str(path),
            "quick_check": quick,
            "integrity_check": integrity,
            "foreign_key_violation_count": len(violations),
            "classified_count": sum(cause_counts.values()),
            "root_cause_counts": dict(sorted(cause_counts.items())),
            "families": families,
            "violations": rows,
        }


def main() -> int:
    args = parse_args()
    if not args.database.is_file():
        raise SystemExit(f"database not found: {args.database}")
    result = classify(args.database, args.include_identifiers)
    if result["classified_count"] != result["foreign_key_violation_count"]:
        raise SystemExit("classification count mismatch")
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(
            f"violations={result['foreign_key_violation_count']} "
            f"classified={result['classified_count']} "
            f"quick_check={result['quick_check']} "
            f"integrity_check={result['integrity_check']}"
        )
        for family in result["families"]:
            print(
                "{child_table}.{foreign_key_column}->{parent_table} "
                "count={violation_count} cause={root_cause_family}".format(**family)
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
