#!/usr/bin/env python3
"""Inventory secret-like environment artifacts without exposing values.

Read-only by default. ``--fix-permissions`` only removes group/other permission
bits from regular files; it never moves, deletes, or rewrites file contents.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any


SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__"}
ENV_NAME = re.compile(r"(^\.env(?:[._-].*)?$)|(^.*(?:env).*(?:bak|backup|copy|old).*$)", re.I)
KEY_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

CATEGORY_MARKERS: dict[str, tuple[str, ...]] = {
    "openai": ("OPENAI",),
    "telegram_api": ("TELEGRAM_API_ID", "TELEGRAM_API_HASH"),
    "telegram_bot": ("BOT_TOKEN", "TELEGRAM_BOT_TOKEN"),
    "database": ("DATABASE_URL", "DB_PASSWORD", "POSTGRES_PASSWORD"),
    "session_or_jwt": ("SECRET_KEY", "SESSION_SECRET", "JWT_SECRET"),
    "operator_or_admin": ("ADMIN_TOKEN", "DASHBOARD_ADMIN_TOKEN", "OPERATOR_TOKEN"),
    "oauth_or_social": ("CLIENT_SECRET", "OAUTH", "FACEBOOK", "INSTAGRAM", "LINKEDIN", "TIKTOK"),
    "provider_api": ("API_KEY", "SIGNING_SECRET", "WEBHOOK_SECRET"),
    "storage": ("AWS_", "S3_", "STORAGE_"),
    "rpc_or_explorer": ("RPC_", "ETHERSCAN", "BSCSCAN", "TRONGRID"),
    "wallet_or_key_material": ("SEED", "PRIVATE_KEY", "XPUB", "HOT_WALLET", "KMS", "HSM"),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("roots", nargs="+", type=Path, help="Directories to inspect")
    parser.add_argument("--repo", type=Path, help="Git repository used for tracking status")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument(
        "--fix-permissions",
        action="store_true",
        help="Change regular secret-like files to owner read/write only",
    )
    return parser.parse_args()


def iter_candidates(root: Path):
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if ENV_NAME.match(name):
                yield Path(current) / name


def safe_key_names(path: Path) -> set[str]:
    keys: set[str] = set()
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                stripped = raw.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key = stripped.split("=", 1)[0].strip()
                if KEY_NAME.match(key):
                    keys.add(key)
    except (OSError, UnicodeError):
        pass
    return keys


def categories(keys: set[str]) -> list[str]:
    result: set[str] = set()
    for category, markers in CATEGORY_MARKERS.items():
        for key in keys:
            if any(marker in key.upper() for marker in markers):
                result.add(category)
                break
    return sorted(result)


def fingerprint(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def is_tracked(path: Path, repo: Path | None) -> bool:
    if repo is None:
        return False
    try:
        relative = path.resolve().relative_to(repo.resolve())
    except ValueError:
        return False
    result = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--error-unmatch", "--", str(relative)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def inspect(path: Path, root: Path, repo: Path | None, fix: bool) -> dict[str, Any]:
    before = path.stat()
    before_mode = stat.S_IMODE(before.st_mode)
    is_example = path.name in {".env.example", ".env.sample", ".env.template"}
    keys = safe_key_names(path)
    secret_categories = categories(keys)
    is_secret_artifact = bool(secret_categories) and not is_example
    changed = False
    if fix and is_secret_artifact and stat.S_ISREG(before.st_mode) and before_mode & 0o077:
        path.chmod(before_mode & ~0o077)
        changed = True
    after = path.stat()
    resolved = path.resolve()
    active = resolved == root / ".env"
    return {
        "path": str(path),
        "tracked": is_tracked(path, repo),
        "mode_before": f"{before_mode:04o}",
        "mode_after": f"{stat.S_IMODE(after.st_mode):04o}",
        "owner_uid": before.st_uid,
        "group_gid": before.st_gid,
        "size_bytes": before.st_size,
        "modified_epoch": int(before.st_mtime),
        "active_runtime_dependency_candidate": active,
        "likely_secret_categories": secret_categories,
        "variable_name_count": len(keys),
        "is_example": is_example,
        "is_secret_artifact": is_secret_artifact,
        "duplicate_content_fingerprint": fingerprint(path),
        "safe_to_chmod": is_secret_artifact and stat.S_ISREG(before.st_mode),
        "safe_to_quarantine": not active and not is_tracked(path, repo),
        "requires_rotation": is_secret_artifact,
        "requires_history_review": is_tracked(path, repo) and is_secret_artifact,
        "permission_changed": changed,
    }


def main() -> int:
    args = parse_args()
    roots = [root.resolve() for root in args.roots]
    missing = [str(root) for root in roots if not root.is_dir()]
    if missing:
        raise SystemExit(f"missing roots: {', '.join(missing)}")
    rows = [
        inspect(path, root, args.repo, args.fix_permissions)
        for root in roots
        for path in sorted(set(iter_candidates(root)))
    ]
    # Wave F/I: shared /opt/autostory/.env is root:storyfleet 0640 so the
    # dedicated service user can read EnvironmentFile without world access.
    # Flag only world-accessible secret modes (other bits), not group-read.
    insecure = [
        row
        for row in rows
        if row["is_secret_artifact"] and int(row["mode_after"], 8) & 0o007
    ]
    summary = {
        "artifact_count": len(rows),
        "tracked_count": sum(1 for row in rows if row["tracked"]),
        "insecure_permission_count": len(insecure),
        "permission_changes": sum(1 for row in rows if row["permission_changed"]),
        "artifacts": rows,
    }
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(
            "artifacts={artifact_count} tracked={tracked_count} "
            "insecure_permissions={insecure_permission_count} "
            "permission_changes={permission_changes}".format(**summary)
        )
        for row in rows:
            print(
                f"{row['path']}\ttracked={row['tracked']}\t"
                f"mode={row['mode_after']}\tcategories={','.join(row['likely_secret_categories']) or 'none'}"
            )
    return 2 if insecure else 0


if __name__ == "__main__":
    raise SystemExit(main())
