#!/usr/bin/env python3
"""
Convert a Telethon .session file (SQLite) to a session string.
Use when you have session/12345.session from an export (e.g. 50-us-27.01.zip)
and need the string for Dashboard → Add Account → Import from tdata.

Usage:
  python scripts/session_to_string.py /path/to/session/15132239764.session
  python scripts/session_to_string.py /path/to/session  # converts all .session in folder

Prints the session string(s) to stdout.
"""
import sys
from pathlib import Path

# Project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.tdata_convert import session_file_to_string


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/session_to_string.py <path-to.session> [path2.session ...]")
        print("   or: python scripts/session_to_string.py <path-to-folder-with-session-files>")
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
            print(f"# Skip {p.name}: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
