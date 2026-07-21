#!/usr/bin/env python3
"""Rehearse Telegram session encryption on an explicit disposable DB copy."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.security.session_material import SessionMaterialService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("--expected-plaintext", type=int)
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


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def run(args: argparse.Namespace) -> dict:
    target = args.database.resolve()
    if not target.is_file():
        raise SystemExit(f"database not found: {target}")
    if any(same_file(target, protected) for protected in args.protected_path):
        raise SystemExit("refusing protected/live database target")
    if args.apply and not args.acknowledge_disposable_copy:
        raise SystemExit("--apply requires --acknowledge-disposable-copy")

    service = SessionMaterialService.from_environment()
    if args.apply and service.mode != "transition":
        raise SystemExit("migration apply requires TELEGRAM_SESSION_ENCRYPTION_MODE=transition")

    with sqlite3.connect(target) as db:
        rows = list(
            db.execute(
                "SELECT id, session_string FROM accounts "
                "WHERE session_string IS NOT NULL AND session_string <> '' ORDER BY id"
            )
        )
        plaintext: list[tuple[int, str]] = []
        encrypted = 0
        for account_id, stored in rows:
            if service.is_encrypted(stored):
                service.decrypt(stored)
                encrypted += 1
            else:
                plaintext.append((int(account_id), str(stored)))
        if (
            args.expected_plaintext is not None
            and len(plaintext) != args.expected_plaintext
        ):
            raise SystemExit(
                "unexpected plaintext count: "
                f"expected={args.expected_plaintext} actual={len(plaintext)}"
            )
        if not args.apply:
            return {
                "mode": "dry-run",
                "database": str(target),
                "session_rows": len(rows),
                "plaintext_rows": len(plaintext),
                "encrypted_rows": encrypted,
                "would_migrate": len(plaintext),
            }

        originals = {account_id: digest(value) for account_id, value in plaintext}
        db.execute("BEGIN IMMEDIATE")
        try:
            for account_id, value in plaintext:
                encrypted_value = service.encrypt(value)
                cursor = db.execute(
                    "UPDATE accounts SET session_string=? "
                    "WHERE id=? AND session_string=?",
                    (encrypted_value, account_id, value),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(
                        f"concurrent session update detected for account id {account_id}"
                    )
            db.commit()
        except Exception:
            db.rollback()
            raise

        migrated = 0
        remaining_plaintext = 0
        for account_id, stored in db.execute(
            "SELECT id, session_string FROM accounts "
            "WHERE session_string IS NOT NULL AND session_string <> '' ORDER BY id"
        ):
            if not service.is_encrypted(stored):
                remaining_plaintext += 1
                continue
            decrypted = service.decrypt(stored)
            if int(account_id) in originals:
                if digest(decrypted) != originals[int(account_id)]:
                    raise RuntimeError(
                        f"post-migration verification failed for account id {account_id}"
                    )
                migrated += 1
        return {
            "mode": "apply",
            "database": str(target),
            "session_rows": len(rows),
            "plaintext_rows_before": len(plaintext),
            "encrypted_rows_before": encrypted,
            "migrated_rows": migrated,
            "plaintext_rows_after": remaining_plaintext,
            "verification": "passed",
        }


def main() -> int:
    args = parse_args()
    result = run(args)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(" ".join(f"{key}={value}" for key, value in result.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
