#!/usr/bin/env python3
"""Guarded Telegram session encryption migration tooling.

Defaults to dry-run. Refuses known production database paths unless
--allow-production is supplied together with explicit acknowledgements.
Never prints session material, keys, nonces, or ciphertext.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.security.session_material import (  # noqa: E402
    SessionMaterialService,
    reset_session_aad_account_id,
    session_aad_account_id,
    _bump,
)

DEFAULT_PROTECTED = [
    Path("/opt/autostory/data/storyfleet.db"),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=["inspect", "dry-run", "migrate", "verify", "reencrypt", "rollback-check"],
    )
    parser.add_argument("database", type=Path)
    parser.add_argument("--expected-plaintext", type=int)
    parser.add_argument("--expected-encrypted", type=int)
    parser.add_argument("--expected-total", type=int)
    parser.add_argument("--protected-path", type=Path, action="append", default=[])
    parser.add_argument("--allow-production", action="store_true")
    parser.add_argument("--acknowledge-disposable-copy", action="store_true")
    parser.add_argument("--acknowledge-production-migration", action="store_true")
    parser.add_argument("--bind-account-aad", action="store_true", default=False,
                        help="Bind AAD to account id (must match readers; not compatible with EncryptedSessionText)")
    parser.add_argument("--target-key-id", type=str, default="")
    parser.add_argument(
        "--materialize-filesystem-sessions",
        action="store_true",
        help="Convert path-like session_string / session_path Telethon files to StringSession before encrypt",
    )
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def same_file(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve() or left.samefile(right)
    except (FileNotFoundError, OSError):
        return left.resolve() == right.resolve()



def _looks_like_path(value: str) -> bool:
    s = (value or "").strip()
    if not s:
        return False
    return (
        s.startswith("/")
        or s.startswith("file://")
        or s.endswith(".session")
        or "\\" in s
        or "\\" in s
    )


def materialize_plaintext(account_id: int, value: str, session_path: str | None) -> tuple[str, str]:
    """Return (plaintext_to_encrypt, source_kind). Never logs material."""
    from src.core.tdata_convert import session_file_to_string
    from src.core.session_paths import get_canonical_session_path

    candidates: list[Path] = []
    raw = value.strip()
    if _looks_like_path(raw):
        candidates.append(Path(raw.replace("file://", "", 1)).expanduser())
    if session_path and str(session_path).strip():
        candidates.append(Path(str(session_path).strip()).expanduser())
    try:
        candidates.append(get_canonical_session_path(int(account_id)))
    except Exception:
        pass
    seen = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            if path.is_file():
                return session_file_to_string(path), "filesystem_materialized"
            alt = path if path.suffix.lower() == ".session" else Path(str(path) + ".session")
            if alt.is_file():
                return session_file_to_string(alt), "filesystem_materialized"
        except Exception as exc:
            raise RuntimeError(
                f"filesystem materialization failed for account id {account_id}: {type(exc).__name__}"
            ) from exc
    if _looks_like_path(raw):
        raise RuntimeError(f"filesystem session missing for account id {account_id}")
    return value, "legacy_string"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def assert_target_allowed(target: Path, args: argparse.Namespace) -> None:
    protected = list(DEFAULT_PROTECTED) + list(args.protected_path or [])
    is_protected = any(same_file(target, p) for p in protected if p)
    mutating = args.command in {"migrate", "reencrypt"}
    if is_protected:
        if not args.allow_production:
            raise SystemExit("refusing protected/live database target (pass --allow-production only with explicit authorization)")
        if mutating and not args.acknowledge_production_migration:
            raise SystemExit("production mutation requires --acknowledge-production-migration")
    if mutating and not is_protected and not args.acknowledge_disposable_copy:
        raise SystemExit("mutation requires --acknowledge-disposable-copy for non-production targets")


def classify_rows(db: sqlite3.Connection, service: SessionMaterialService) -> dict:
    rows = list(
        db.execute(
            "SELECT id, session_string FROM accounts "
            "WHERE session_string IS NOT NULL AND session_string <> '' ORDER BY id"
        )
    )
    plaintext = []
    encrypted = []
    unknown = []
    for account_id, stored in rows:
        info = service.inspect_format(stored)
        fmt = info["format"]
        if fmt == "enc_v1_aes256gcm":
            # Ensure decrypt works with optional account AAD
            token = None
            try:
                if _BIND:
                    token = session_aad_account_id(int(account_id))
                service.decrypt(str(stored))
                encrypted.append(int(account_id))
            except Exception:
                unknown.append(int(account_id))
            finally:
                if token is not None:
                    reset_session_aad_account_id(token)
        elif fmt in {"legacy_plaintext", "legacy_path"}:
            plaintext.append(int(account_id))
        else:
            unknown.append(int(account_id))
    null_rows = db.execute(
        "SELECT count(*) FROM accounts WHERE session_string IS NULL OR session_string = ''"
    ).fetchone()[0]
    return {
        "session_rows": len(rows),
        "plaintext_rows": len(plaintext),
        "encrypted_v1_rows": len(encrypted),
        "unknown_format_rows": len(unknown),
        "null_or_empty_rows": int(null_rows),
        "plaintext_ids_count": len(plaintext),
        "encrypted_ids_count": len(encrypted),
        "_plaintext_ids": plaintext,
        "_encrypted_ids": encrypted,
    }


_BIND = False


def args_no_bind() -> bool:
    return not _BIND


def run(args: argparse.Namespace) -> dict:
    global _BIND
    _BIND = bool(args.bind_account_aad)
    target = args.database.resolve()
    if not target.is_file():
        raise SystemExit(f"database not found: {target}")
    assert_target_allowed(target, args)

    service = SessionMaterialService.from_environment()
    meta = service.safe_key_metadata()

    with sqlite3.connect(target) as db:
        classified = classify_rows(db, service)
        result = {
            "command": args.command,
            "database": str(target),
            "database_hash": file_digest(target),
            "active_key_id": meta.get("active_key_id"),
            "mode": service.mode,
            "session_rows": classified["session_rows"],
            "plaintext_rows": classified["plaintext_rows"],
            "encrypted_v1_rows": classified["encrypted_v1_rows"],
            "unknown_format_rows": classified["unknown_format_rows"],
            "null_or_empty_rows": classified["null_or_empty_rows"],
            "key_metadata": meta,
        }

        if args.expected_plaintext is not None and classified["plaintext_rows"] != args.expected_plaintext:
            raise SystemExit(
                f"unexpected plaintext count: expected={args.expected_plaintext} actual={classified['plaintext_rows']}"
            )
        if args.expected_encrypted is not None and classified["encrypted_v1_rows"] != args.expected_encrypted:
            raise SystemExit(
                f"unexpected encrypted count: expected={args.expected_encrypted} actual={classified['encrypted_v1_rows']}"
            )
        if args.expected_total is not None and classified["session_rows"] != args.expected_total:
            raise SystemExit(
                f"unexpected session row count: expected={args.expected_total} actual={classified['session_rows']}"
            )

        if args.command in {"inspect", "dry-run", "rollback-check"}:
            path_like = 0
            opaque = 0
            for account_id, stored in db.execute(
                "SELECT id, session_string FROM accounts "
                "WHERE session_string IS NOT NULL AND session_string <> ''"
            ):
                if service.is_encrypted(str(stored)):
                    continue
                if _looks_like_path(str(stored)):
                    path_like += 1
                else:
                    opaque += 1
            result["would_migrate"] = classified["plaintext_rows"]
            result["plaintext_path_like_rows"] = path_like
            result["plaintext_opaque_string_rows"] = opaque
            result["materialize_requested"] = bool(args.materialize_filesystem_sessions)
            result["status"] = "ok"
            return result

        if args.command == "verify":
            # All encrypted must decrypt; plaintext allowed only outside encrypted-only
            failures = 0
            for account_id, stored in db.execute(
                "SELECT id, session_string FROM accounts "
                "WHERE session_string IS NOT NULL AND session_string <> ''"
            ):
                token = None
                try:
                    if _BIND:
                        token = session_aad_account_id(int(account_id))
                    service.decrypt(str(stored))
                except Exception:
                    failures += 1
                finally:
                    if token is not None:
                        reset_session_aad_account_id(token)
            result["verify_failures"] = failures
            result["status"] = "ok" if failures == 0 else "failed"
            if failures:
                raise SystemExit(f"verify failed: {failures} rows")
            return result

        if service.mode not in {"transition", "encrypted-only"} and args.command in {"migrate", "reencrypt"}:
            # migrate encrypts plaintext → needs a mode that writes ciphertext
            if service.mode == "disabled":
                raise SystemExit("migration apply requires TELEGRAM_SESSION_ENCRYPTION_MODE=transition")

        if args.command == "migrate":
            if service.mode != "transition":
                raise SystemExit("migration apply requires TELEGRAM_SESSION_ENCRYPTION_MODE=transition")
            plaintext_ids = list(classified["_plaintext_ids"])
            originals: dict[int, str] = {}
            # Digests are captured during migrate after optional materialization.

            db.execute("BEGIN IMMEDIATE")
            try:
                migrated = 0
                for account_id in plaintext_ids:
                    row = db.execute(
                        "SELECT session_string FROM accounts WHERE id=?",
                        (account_id,),
                    ).fetchone()
                    if not row:
                        continue
                    value = str(row[0])
                    original_stored = value
                    session_path = None
                    try:
                        sp_row = db.execute(
                            "SELECT session_path FROM accounts WHERE id=?",
                            (account_id,),
                        ).fetchone()
                        if sp_row:
                            session_path = sp_row[0]
                    except sqlite3.OperationalError:
                        session_path = None
                    if args.materialize_filesystem_sessions:
                        value, _src = materialize_plaintext(account_id, value, session_path)
                    originals[int(account_id)] = digest(value)
                    token = session_aad_account_id(account_id) if _BIND else None
                    try:
                        encrypted_value = service.encrypt(value)
                    finally:
                        if token is not None:
                            reset_session_aad_account_id(token)
                    cursor = db.execute(
                        "UPDATE accounts SET session_string=? WHERE id=? AND session_string=?",
                        (encrypted_value, account_id, original_stored),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError(f"concurrent session update detected for account id {account_id}")
                    if args.materialize_filesystem_sessions:
                        try:
                            db.execute(
                                "UPDATE accounts SET session_path=NULL WHERE id=?",
                                (account_id,),
                            )
                        except sqlite3.OperationalError:
                            pass
                    migrated += 1
                    _bump("telegram_session_migrations_total")
                db.commit()
            except Exception:
                db.rollback()
                raise

            # verify digests
            remaining_plaintext = 0
            verified = 0
            for account_id, stored in db.execute(
                "SELECT id, session_string FROM accounts "
                "WHERE session_string IS NOT NULL AND session_string <> ''"
            ):
                if not service.is_encrypted(str(stored)):
                    remaining_plaintext += 1
                    continue
                token = session_aad_account_id(int(account_id)) if _BIND else None
                try:
                    decrypted = service.decrypt(str(stored))
                finally:
                    if token is not None:
                        reset_session_aad_account_id(token)
                if int(account_id) in originals:
                    if digest(decrypted) != originals[int(account_id)]:
                        raise RuntimeError(
                            f"post-migration verification failed for account id {account_id}"
                        )
                    verified += 1
            result.update(
                {
                    "migrated_rows": migrated,
                    "verified_rows": verified,
                    "plaintext_rows_after": remaining_plaintext,
                    "verification": "passed",
                    "status": "ok",
                }
            )
            return result

        if args.command == "reencrypt":
            target_key = (args.target_key_id or service.active_key_id or "").strip()
            if not target_key:
                raise SystemExit("reencrypt requires active or --target-key-id")
            count = 0
            db.execute("BEGIN IMMEDIATE")
            try:
                for account_id, stored in db.execute(
                    "SELECT id, session_string FROM accounts "
                    "WHERE session_string IS NOT NULL AND session_string <> ''"
                ):
                    if not service.is_encrypted(str(stored)):
                        continue
                    token = session_aad_account_id(int(account_id)) if _BIND else None
                    try:
                        new_val = service.reencrypt(str(stored), target_key_id=target_key)
                    finally:
                        if token is not None:
                            reset_session_aad_account_id(token)
                    db.execute(
                        "UPDATE accounts SET session_string=? WHERE id=?",
                        (new_val, int(account_id)),
                    )
                    count += 1
                db.commit()
            except Exception:
                db.rollback()
                raise
            result.update({"reencrypted_rows": count, "status": "ok"})
            return result

        raise SystemExit(f"unsupported command: {args.command}")


def main() -> int:
    args = parse_args()
    # Map legacy alias: dry-run is inspect-like
    if args.command == "dry-run":
        # keep command name in output
        pass
    result = run(args)
    # Strip private keys
    result.pop("_plaintext_ids", None)
    result.pop("_encrypted_ids", None)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(" ".join(f"{key}={value}" for key, value in result.items() if not isinstance(value, (dict, list))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
