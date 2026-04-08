"""
Lightweight DB migration script for AutoStory.

Adds columns to existing SQLite databases that predate the model changes.
Safe to run multiple times — skips columns that already exist.

Usage:
    python scripts/migrate_db.py
"""
import os
import sys
import sqlite3

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.abspath(os.path.join(_here, ".."))
sys.path.insert(0, _root)

from config.settings import settings

DB_PATH = settings.database.url.replace("sqlite:///", "")
if not os.path.isabs(DB_PATH):
    DB_PATH = os.path.join(_root, DB_PATH)


def column_exists(cur, table: str, column: str) -> bool:
    cur.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


MIGRATIONS = [
    # (table, column, definition)
    ("accounts", "health_status",   "TEXT"),
    ("accounts", "health_reason",   "TEXT"),
    ("accounts", "health_checked_at", "DATETIME"),
    ("accounts", "purpose",         "TEXT DEFAULT 'both'"),
]


def main():
    print(f"DB: {DB_PATH}")
    if not os.path.exists(DB_PATH):
        print("Database file not found — nothing to migrate (create_all will handle new installs).")
        return

    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    applied = 0
    for table, col, defn in MIGRATIONS:
        if column_exists(cur, table, col):
            print(f"  [skip]  {table}.{col} already exists")
        else:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {defn}")
            print(f"  [added] {table}.{col} {defn}")
            applied += 1
    con.commit()
    con.close()
    print(f"Done — {applied} column(s) added.")


if __name__ == "__main__":
    main()
