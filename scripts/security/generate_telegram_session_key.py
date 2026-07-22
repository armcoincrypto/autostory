#!/usr/bin/env python3
"""Generate a 256-bit Telegram session encryption key into a 0600 env file.

Does not print the key. Writes ACTIVE_KEY_ID + KEY_B64 lines only.
"""
from __future__ import annotations

import argparse
import base64
import os
import stat
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--key-id", default="v1")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    out: Path = args.output
    if out.exists() and not args.force:
        print(f"refusing to overwrite existing file: {out}", file=sys.stderr)
        return 2
    if not args.key_id or len(args.key_id) > 64:
        print("invalid key id", file=sys.stderr)
        return 2
    key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    out.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "# Autostory Telegram session encryption key ring (mode 0600). Do not commit.\n"
        f"TELEGRAM_SESSION_ENCRYPTION_ACTIVE_KEY_ID={args.key_id}\n"
        f"TELEGRAM_SESSION_ENCRYPTION_KEY_B64={key}\n"
    )
    fd = os.open(str(out), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, content.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(out, 0o600)
    mode = stat.S_IMODE(out.stat().st_mode)
    print(f"wrote_key_file={out} mode={oct(mode)} key_id={args.key_id} key_material=redacted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
