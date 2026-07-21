"""Dependency-light, read-only helpers for locating and inspecting sessions."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from src.clients.session_resolve import _strip_file_url
from src.core.session_paths import get_canonical_session_path


def resolve_existing_session_path(account: Any) -> tuple[bool, str | None]:
    """Return the first existing session path using canonical resolver order."""
    session_string = (getattr(account, "session_string", None) or "").strip()
    session_path = (getattr(account, "session_path", None) or "").strip()
    account_id = getattr(account, "id", None)

    paths: list[Path] = []
    if session_string:
        paths.append(Path(_strip_file_url(session_string)).expanduser())
    if session_path:
        paths.append(Path(_strip_file_url(session_path)).expanduser())
    if account_id is not None:
        paths.append(get_canonical_session_path(int(account_id)))

    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            if path.is_file():
                return True, str(path.resolve())
        except OSError:
            continue
    return False, None


def sqlite_schema_version_readonly(
    path: Path,
) -> tuple[int | None, int | None, list[str]]:
    """Read Telethon SQLite schema metadata without opening it via Telethon."""
    warnings: list[str] = []
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        warnings.append(f"sqlite_open_failed:{type(exc).__name__}")
        return None, None, warnings
    try:
        version_row = connection.execute(
            "SELECT version FROM version LIMIT 1"
        ).fetchone()
        db_version = (
            int(version_row[0])
            if version_row and version_row[0] is not None
            else None
        )
        columns = [
            row[1]
            for row in connection.execute("PRAGMA table_info(sessions)").fetchall()
        ]
        table_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "sessions" not in table_names:
            warnings.append("missing_sessions_table")
        return db_version, len(columns) if columns else None, warnings
    except sqlite3.Error as exc:
        warnings.append(f"sqlite_read_failed:{type(exc).__name__}")
        return None, None, warnings
    finally:
        connection.close()

