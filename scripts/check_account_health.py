#!/usr/bin/env python3
"""
Test harness for "Check accounts alive" — run with verbose logging for a single account.

Use this to reproduce and debug false "alive" on an account that is actually deleted.

Usage — run from the project root (the directory that contains main.py and src/):

  # Direct script (no "scripts" package needed; use real account ID e.g. 1)
  python scripts/check_account_health.py --account-id 1 --verbose

  # Or as module
  python -m scripts.check_account_health --account-id 1 --verbose

  # Check all accounts
  python scripts/check_account_health.py

On VPS: cd to your autostory deploy directory first (not /root if that's another project).
Use a real number for --account-id (e.g. 1), not the literal "<ID>".
Logs go to stderr (verbose). Results and reason_code printed to stdout.
"""
import argparse
import asyncio
import sys
from pathlib import Path

# Project root (so "src" and "config" are importable when run as script)
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
load_dotenv(_ROOT / ".env")
load_dotenv()


def main():
    parser = argparse.ArgumentParser(
        description="Check account(s) health with optional verbose logging",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--account-id",
        type=int,
        default=None,
        help="Check only this account ID (default: all)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Emit step-by-step and exception logs (safe, no secrets)",
    )
    parser.add_argument(
        "--update-status",
        action="store_true",
        help="Update account status in DB for non-alive accounts",
    )
    args = parser.parse_args()

    # Configure structlog so verbose logs appear when running as script
    if args.verbose:
        import structlog
        structlog.configure(
            processors=[
                structlog.stdlib.filter_by_level,
                structlog.stdlib.add_logger_name,
                structlog.stdlib.add_log_level,
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.dev.ConsoleRenderer(colors=True),
            ],
            wrapper_class=structlog.stdlib.BoundLogger,
            context_class=dict,
            logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        )

    from src.core.database import init_db
    from src.clients.manager import client_manager

    init_db()

    account_ids = [args.account_id] if args.account_id is not None else None

    async def run():
        return await client_manager.check_accounts_health(
            update_status=args.update_status,
            account_ids=account_ids,
            verbose=args.verbose,
        )

    results = asyncio.run(run())

    # Print summary
    print("\n" + "=" * 60)
    print("         Account health check")
    print("=" * 60)
    alive = sum(1 for r in results if r["status"] == "alive")
    deleted = sum(1 for r in results if r["status"] == "deleted")
    banned = sum(1 for r in results if r["status"] == "banned")
    restricted = sum(1 for r in results if r["status"] == "restricted")
    auth_required = sum(1 for r in results if r["status"] == "auth_required")
    flood_wait = sum(1 for r in results if r["status"] == "flood_wait")
    errors = sum(1 for r in results if r["status"] == "error")
    print(f"\n  Alive:          {alive}")
    print(f"  Deleted:        {deleted}")
    print(f"  Banned:        {banned}")
    print(f"  Restricted:    {restricted}")
    print(f"  Auth required: {auth_required}")
    print(f"  Flood wait:    {flood_wait}")
    print(f"  Error:         {errors}")
    print("\n  Per account (status, reason_code, message):")
    for r in results:
        reason = r.get("reason_code", "-")
        print(f"    #{r['account_id']} {r['phone']}: {r['status']} | reason_code={reason} | {r.get('message', '')}")
    print("\n" + "=" * 60 + "\n")
    return 0 if results else 1


if __name__ == "__main__":
    sys.exit(main())
