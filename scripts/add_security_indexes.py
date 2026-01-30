#!/usr/bin/env python3
"""
SECURITY HOTFIX: Add critical database indexes for performance
Run this script after deploying security patches
"""
import sqlite3
import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))


def add_security_indexes(db_path: str = None):
    """
    Add missing indexes to improve performance and query security

    Indexes added:
    1. discovered_users.times_mentioned - for finding available users
    2. discovered_users.username - for mention lookups
    3. accounts.status - for filtering active accounts
    4. stories.published_at - for sorting/filtering stories
    5. accounts.last_action_at - for rate limiting
    """

    # Find database path
    if not db_path:
        # Try common locations
        possible_paths = [
            '/opt/autostory/autostory.db',
            './autostory.db',
            '../autostory.db',
            str(Path(__file__).parent.parent / 'autostory.db'),
        ]
        for path in possible_paths:
            if os.path.exists(path):
                db_path = path
                break

    if not db_path or not os.path.exists(db_path):
        print(f"Database not found. Tried: {possible_paths}")
        return 0

    print(f"Updating database: {db_path}")

    # Index definitions
    indexes = [
        # Performance indexes for common queries
        (
            "ix_discovered_users_times_mentioned",
            "CREATE INDEX IF NOT EXISTS ix_discovered_users_times_mentioned "
            "ON discovered_users(times_mentioned)"
        ),
        (
            "ix_discovered_users_username_notnull",
            "CREATE INDEX IF NOT EXISTS ix_discovered_users_username_notnull "
            "ON discovered_users(username) WHERE username IS NOT NULL"
        ),
        (
            "ix_discovered_users_available",
            "CREATE INDEX IF NOT EXISTS ix_discovered_users_available "
            "ON discovered_users(times_mentioned, username) "
            "WHERE times_mentioned = 0 AND username IS NOT NULL"
        ),
        (
            "ix_accounts_status",
            "CREATE INDEX IF NOT EXISTS ix_accounts_status "
            "ON accounts(status)"
        ),
        (
            "ix_accounts_active_sessions",
            "CREATE INDEX IF NOT EXISTS ix_accounts_active_sessions "
            "ON accounts(status, session_string) "
            "WHERE status = 'active' AND session_string IS NOT NULL"
        ),
        (
            "ix_stories_published_at",
            "CREATE INDEX IF NOT EXISTS ix_stories_published_at "
            "ON stories(published_at DESC)"
        ),
        (
            "ix_stories_account_id",
            "CREATE INDEX IF NOT EXISTS ix_stories_account_id "
            "ON stories(account_id)"
        ),
        # Security/audit indexes
        (
            "ix_accounts_last_action",
            "CREATE INDEX IF NOT EXISTS ix_accounts_last_action "
            "ON accounts(last_action_at)"
        ),
        (
            "ix_discovered_users_last_mentioned",
            "CREATE INDEX IF NOT EXISTS ix_discovered_users_last_mentioned "
            "ON discovered_users(last_mentioned_at) "
            "WHERE last_mentioned_at IS NOT NULL"
        ),
    ]

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    try:
        # Enable WAL mode for better concurrency
        print("Enabling WAL mode...")
        cursor.execute("PRAGMA journal_mode = WAL")
        result = cursor.fetchone()
        print(f"  Journal mode: {result[0]}")

        # Set synchronous mode for better performance
        cursor.execute("PRAGMA synchronous = NORMAL")
        print("  Synchronous: NORMAL")

        # Add indexes
        added = 0
        for index_name, sql in indexes:
            try:
                # Check if index exists
                cursor.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='index' AND name=?",
                    (index_name,)
                )
                exists = cursor.fetchone()

                if not exists:
                    print(f"Adding index: {index_name}")
                    cursor.execute(sql)
                    added += 1
                else:
                    print(f"Index exists: {index_name}")

            except sqlite3.Error as e:
                print(f"Error adding {index_name}: {e}")

        conn.commit()

        # Run ANALYZE to update statistics
        print("\nRunning ANALYZE...")
        cursor.execute("ANALYZE")
        conn.commit()

        # Report results
        print(f"\nAdded {added} new indexes")

        # Show all indexes
        print("\nCurrent indexes:")
        cursor.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name LIKE 'ix_%' "
            "ORDER BY name"
        )
        for row in cursor.fetchall():
            print(f"  - {row[0]}")

        return added

    except Exception as e:
        print(f"Error: {e}")
        return 0

    finally:
        conn.close()


def check_database_health(db_path: str = None):
    """Run database health checks"""

    if not db_path:
        db_path = '/opt/autostory/autostory.db'

    if not os.path.exists(db_path):
        print(f"Database not found: {db_path}")
        return

    print(f"\nDatabase health check: {db_path}")
    print("=" * 50)

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    try:
        # Check integrity
        print("Running integrity check...")
        cursor.execute("PRAGMA integrity_check")
        result = cursor.fetchone()
        print(f"  Integrity: {result[0]}")

        # Check table sizes
        print("\nTable sizes:")
        tables = ['accounts', 'discovered_users', 'stories', 'campaigns', 'tasks']
        for table in tables:
            try:
                cursor.execute(f"SELECT COUNT(*) FROM {table}")
                count = cursor.fetchone()[0]
                print(f"  {table}: {count:,} rows")
            except sqlite3.Error:
                print(f"  {table}: (not found)")

        # Check journal mode
        cursor.execute("PRAGMA journal_mode")
        print(f"\nJournal mode: {cursor.fetchone()[0]}")

        # Check page size
        cursor.execute("PRAGMA page_size")
        print(f"Page size: {cursor.fetchone()[0]} bytes")

        # Check cache size
        cursor.execute("PRAGMA cache_size")
        print(f"Cache size: {cursor.fetchone()[0]} pages")

    finally:
        conn.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Add security indexes to database")
    parser.add_argument("--db", help="Path to database file")
    parser.add_argument("--health", action="store_true", help="Run health check")
    args = parser.parse_args()

    if args.health:
        check_database_health(args.db)
    else:
        added = add_security_indexes(args.db)
        print(f"\nDone. Added {added} indexes.")

        # Also run health check
        check_database_health(args.db)
