"""Read-only SQLite session backup into a disposable destination.

Used by fleet certification and readiness deep probes so Telethon metadata
writes never touch the canonical account session file.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


def copy_sqlite_session_readonly(source: Path, destination: Path) -> None:
    """Consistent SQLite backup from a read-only source into ``destination``."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=5) as src:
        with sqlite3.connect(destination, timeout=5) as dst:
            src.backup(dst)
