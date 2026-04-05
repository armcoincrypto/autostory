#!/usr/bin/env python3
"""
Backfill safety/warmup metadata for legacy accounts.
Sets imported_at from created_at when missing; infers import_source from existing data.
Supports --dry-run to preview changes.
"""
import argparse
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from datetime import datetime
from sqlalchemy import text
from src.core.database import engine, init_db


def main():
    parser = argparse.ArgumentParser(description="Backfill safety metadata for legacy accounts")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without writing")
    args = parser.parse_args()
    init_db()
    now = datetime.utcnow().isoformat()
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT id, created_at, imported_at, import_source FROM accounts")
        ).fetchall()
    updates = []
    for r in rows:
        aid, created_at, imported_at, import_source = r
        if imported_at is None and created_at:
            imported_at = created_at
            updates.append((aid, imported_at.isoformat() if hasattr(imported_at, "isoformat") else str(imported_at), import_source or "legacy"))
    if not updates:
        print("No accounts need backfill.")
        return
    print(f"Would update {len(updates)} accounts: set imported_at from created_at, import_source=legacy")
    for aid, imp, src in updates[:10]:
        print(f"  id={aid} imported_at={imp} import_source={src}")
    if len(updates) > 10:
        print(f"  ... and {len(updates) - 10} more")
    if args.dry_run:
        print("(Dry run; no changes written)")
        return
    with engine.connect() as conn:
        for aid, imp, src in updates:
            conn.execute(
                text("UPDATE accounts SET imported_at = :imp, import_source = COALESCE(import_source, :src) WHERE id = :aid"),
                {"imp": imp, "src": src, "aid": aid},
            )
        conn.commit()
    print(f"Updated {len(updates)} accounts.")


if __name__ == "__main__":
    main()
