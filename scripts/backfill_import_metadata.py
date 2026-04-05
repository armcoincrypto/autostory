#!/usr/bin/env python3
"""
One-time backfill of import metadata for legacy accounts.

For accounts with canonical session files but null imported_at, first_seen_at,
warmup_status, or import_source, populate conservative defaults.

- first_seen_at: created_at if present, else file mtime of canonical session
- imported_at: created_at or file mtime (never leave null after backfill)
- warmup_status: warmed if account is old enough (imported >7 days ago or has stories)
- import_source: 'legacy' for unknown historical accounts

Idempotent: skips accounts that already have imported_at set.
Run once after deploying import metadata changes.

Usage:
  python scripts/backfill_import_metadata.py [--dry-run] [--account-ids 1,2,3]
"""
import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

_project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_project_root))

from src.core.database import get_db_context, init_db
from src.core.models import Account
from src.core.session_paths import get_canonical_session_path, get_sessions_dir


def _file_mtime_safe(path: Path) -> datetime | None:
    """Return file mtime as naive UTC datetime, or None."""
    try:
        if path.is_file():
            m = path.stat().st_mtime
            return datetime.utcfromtimestamp(m)
    except Exception:
        pass
    return None


def backfill_account(acc: Account, dry_run: bool) -> dict:
    """Backfill metadata for one account. Returns {updated: bool, changes: list}."""
    changes = []
    updated = False

    canonical = get_canonical_session_path(acc.id)
    if not canonical.is_file():
        return {"updated": False, "changes": ["no_canonical_session"]}

    # Idempotent: if imported_at is already set, skip unless --force
    if getattr(acc, "imported_at", None) is not None and not hasattr(backfill_account, "_force"):
        return {"updated": False, "changes": ["already_has_imported_at"]}

    now = datetime.utcnow()
    file_mtime = _file_mtime_safe(canonical)
    created = getattr(acc, "created_at", None)
    created_dt = created if hasattr(created, "year") else None

    # first_seen_at: only set if null
    first_seen = getattr(acc, "first_seen_at", None)
    if first_seen is None:
        candidate = created_dt or file_mtime or now
        changes.append(f"first_seen_at={candidate.isoformat()}")
        if not dry_run:
            acc.first_seen_at = candidate
        updated = True

    # imported_at: only set if null
    imported = getattr(acc, "imported_at", None)
    if imported is None:
        candidate = created_dt or file_mtime or now
        changes.append(f"imported_at={candidate.isoformat()}")
        if not dry_run:
            acc.imported_at = candidate
        updated = True

    # warmup_status: default to warmed for legacy (old or has stories)
    warmup = getattr(acc, "warmup_status", None)
    if warmup is None or warmup == "":
        imported_at_val = getattr(acc, "imported_at", None) or created_dt or file_mtime or now
        has_stories = (getattr(acc, "successful_story_count", 0) or 0) > 0
        old_enough = False
        if imported_at_val:
            try:
                dt = imported_at_val if hasattr(imported_at_val, "year") else datetime.fromisoformat(str(imported_at_val).replace("Z", "").split("+")[0])
                old_enough = (now - dt).days >= 7
            except Exception:
                pass
        new_status = "warmed" if (old_enough or has_stories) else "warming"
        changes.append(f"warmup_status={new_status}")
        if not dry_run:
            acc.warmup_status = new_status
        updated = True

    # import_source: only set if null/empty
    src = getattr(acc, "import_source", None)
    if not src or not str(src).strip():
        changes.append("import_source=legacy")
        if not dry_run:
            acc.import_source = "legacy"
        updated = True

    return {"updated": updated, "changes": changes}


def main():
    parser = argparse.ArgumentParser(description="Backfill import metadata for legacy accounts")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to DB")
    parser.add_argument("--account-ids", type=str, help="Comma-separated account IDs (default: all with canonical session)")
    parser.add_argument("--force", action="store_true", help="Overwrite even when imported_at is set (use with care)")
    args = parser.parse_args()

    init_db()

    with get_db_context() as db:
        if args.account_ids:
            ids = [int(x.strip()) for x in args.account_ids.split(",") if x.strip().isdigit()]
            accounts = db.query(Account).filter(Account.id.in_(ids)).all() if ids else []
        else:
            accounts = db.query(Account).all()

        # Filter to those with canonical session
        sessions_dir = get_sessions_dir()
        to_process = []
        for a in accounts:
            p = get_canonical_session_path(a.id)
            if p.is_file():
                to_process.append(a)

        if args.force:
            backfill_account._force = True  # noqa

        updated_count = 0
        for a in to_process:
            result = backfill_account(a, dry_run=args.dry_run)
            if result["updated"]:
                updated_count += 1
                print(f"  Account {a.id}: {', '.join(result['changes'])}")
            elif result["changes"]:
                print(f"  Account {a.id}: skip ({result['changes'][0]})")

        if not args.dry_run and updated_count > 0:
            db.commit()
            print(f"\nCommitted {updated_count} account(s).")
        elif args.dry_run:
            print(f"\n[DRY RUN] Would update {updated_count} account(s).")
        else:
            print(f"\nNo changes needed.")

    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
