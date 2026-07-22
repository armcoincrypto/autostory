#!/usr/bin/env python3
"""
Convert a Telethon .session file (SQLite) to a session string.

SECURITY: Refuses to run unless ACKNOWLEDGE_SESSION_STRING_EXPORT=1 is set.
Session strings are secret authentication material — never pipe to logs or tickets.

Usage:
  ACKNOWLEDGE_SESSION_STRING_EXPORT=1 python scripts/session_to_string.py /path/to/file.session
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.tdata_convert import session_file_to_string


def main():
    if os.environ.get("ACKNOWLEDGE_SESSION_STRING_EXPORT", "").strip() != "1":
        print(
            "refusing: set ACKNOWLEDGE_SESSION_STRING_EXPORT=1 to run this export tool "
            "(session strings are secret; prefer migrate_telegram_session_encryption "
            "--materialize-filesystem-sessions on a disposable database copy)",
            file=sys.stderr,
        )
        sys.exit(2)
    if len(sys.argv) < 2:
        print("Usage: ACKNOWLEDGE_SESSION_STRING_EXPORT=1 python scripts/session_to_string.py <path>")
        sys.exit(1)
    arg = Path(sys.argv[1]).expanduser().resolve()
    paths = []
    if arg.is_file():
        paths = [arg]
    elif arg.is_dir():
        paths = sorted(arg.glob("*.session"))
        if not paths:
            print(f"No .session files in {arg}")
            sys.exit(1)
    else:
        paths = [arg.with_suffix(".session") if not str(arg).endswith(".session") else arg]
        if not paths[0].exists():
            print(f"Not found: {sys.argv[1]}")
            sys.exit(1)
    for p in paths:
        try:
            s = session_file_to_string(str(p))
            print(f"\n# {p.name}\n{s}\n")
        except Exception as e:
            print(f"# Skip {p.name}: {type(e).__name__}", file=sys.stderr)


if __name__ == "__main__":
    main()
