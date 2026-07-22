#!/usr/bin/env python3
"""Verify an AutoStory immutable release against its RELEASE_MANIFEST.json."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

PROHIBITED_NAMES = {".git", ".env", "venv", ".venv"}
PROHIBITED_SUFFIXES = (".session",)


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    raise SystemExit(1)


def main(release_path: str) -> None:
    root = Path(release_path).resolve()
    manifest_path = root / "RELEASE_MANIFEST.json"
    if not manifest_path.is_file():
        fail("RELEASE_MANIFEST.json missing")
    manifest = json.loads(manifest_path.read_text())
    req = root / "requirements.txt"
    if not req.is_file():
        fail("requirements.txt missing")
    digest = hashlib.sha256(req.read_bytes()).hexdigest()
    expected = (manifest.get("dependency_lock_hashes") or {}).get("requirements.txt")
    if expected and expected != digest:
        fail(f"requirements.txt hash mismatch {digest} != {expected}")
    for name in ("wsgi.py", "main.py", "scripts/run_readiness_worker.py"):
        if not (root / name).exists():
            fail(f"entrypoint missing: {name}")
    for p in root.rglob("*"):
        if p.name in PROHIBITED_NAMES and p.name != "data":
            # data may be symlink to shared; .git/.env/venv must not exist as real trees
            if p.is_dir() and not p.is_symlink() and p.name in {".git", "venv", ".venv"}:
                fail(f"prohibited directory present: {p}")
            if p.is_file() and p.name == ".env":
                fail("prohibited .env present")
        if p.suffix in PROHIBITED_SUFFIXES and p.is_file() and not p.is_symlink():
            fail(f"prohibited session file: {p}")
    locks = manifest.get("mutation_lock_expected") or {}
    for k, v in locks.items():
        if str(v).lower() != "false":
            fail(f"mutation lock expected false for {k}")
    print("RELEASE_MANIFEST_VERIFY=PASS")
    print(f"release_id={manifest.get('release_id')}")
    print(f"git_sha={manifest.get('git_sha')}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <release_path>", file=sys.stderr)
        raise SystemExit(2)
    main(sys.argv[1])
