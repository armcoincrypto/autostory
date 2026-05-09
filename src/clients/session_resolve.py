"""
Resolve Telethon session objects from Account rows (file-based .session vs StringSession).

Used by ClientManager and StoryPublisher so production uses one consistent code path.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Tuple

from telethon.sessions import SQLiteSession, StringSession
import structlog

logger = structlog.get_logger(__name__)

# Machine-readable codes for logs, API, and delivery failures
ERR_EMPTY_SESSION = "empty_session"
ERR_SESSION_FILE_MISSING = "session_file_missing"
ERR_INVALID_SESSION_FORMAT = "invalid_session_format"


def _strip_file_url(s: str) -> str:
    s = (s or "").strip()
    if s.startswith("file://"):
        s = s[7:]
    return s.strip()


def _looks_like_filesystem_path(raw: str) -> bool:
    """Heuristic: treat as path intent (do not use StringSession for these)."""
    s = (raw or "").strip()
    if not s:
        return False
    if s.startswith("file://"):
        return True
    if s.startswith("/"):
        return True
    if s.startswith("\\\\") or (len(s) > 2 and s[1] == ":"):  # Windows drive
        return True
    if "\\" in s:
        return True
    if s.endswith(".session"):
        return True
    return False


def _sqlite_session_from_file_path(path: Path) -> SQLiteSession:
    """Telethon stores SQLite as <base>.session; normalize if path ends with .session."""
    p = path
    try:
        p = path.resolve()
    except OSError:
        p = path
    if p.suffix.lower() == ".session":
        base = str(p.with_suffix(""))
    else:
        base = str(p)
    return SQLiteSession(base)


def probe_telethon_session_kind(account: Any) -> Tuple[str, Optional[str]]:
    """
    Discover session material for shallow readiness (``deep=0``).

    Same resolution order as ``resolve_telethon_session``, but **does not**
    construct ``SQLiteSession`` / open Telethon session databases (avoids
    crashes on legacy or non-Telethon SQLite files and keeps the path DB-only).

    Returns:
        (session_kind, error_code) with error_code None when material exists.
        session_kind: file | string | empty | unknown
    """
    ss = (getattr(account, "session_string", None) or "").strip()
    session_path_attr = getattr(account, "session_path", None)
    sp = (session_path_attr or "").strip() if session_path_attr else ""
    account_id = getattr(account, "id", None)

    paths_to_try: list[Tuple[str, Path]] = []

    if ss:
        raw = _strip_file_url(ss)
        try:
            paths_to_try.append(("session_string", Path(raw).expanduser()))
        except Exception:
            pass

    if sp:
        try:
            paths_to_try.append(("session_path", Path(_strip_file_url(sp)).expanduser().resolve()))
        except Exception:
            pass

    if account_id is not None:
        try:
            from src.core.session_paths import get_canonical_session_path

            paths_to_try.append(("canonical", get_canonical_session_path(int(account_id))))
        except Exception:
            pass

    seen: set[str] = set()
    for _label, path in paths_to_try:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            if path.is_file():
                return "file", None
        except OSError:
            continue

    if ss:
        raw = _strip_file_url(ss)
        try:
            p = Path(raw).expanduser()
            if _looks_like_filesystem_path(raw) and not p.is_file():
                return "file", ERR_SESSION_FILE_MISSING
        except OSError:
            return "file", ERR_SESSION_FILE_MISSING

        try:
            StringSession(ss)
        except Exception as e:
            logger.info(
                "StringSession rejected",
                account_id=account_id,
                session_kind_guessed="string",
                error=str(e),
            )
            return "string", ERR_INVALID_SESSION_FORMAT
        return "string", None

    return "empty", ERR_EMPTY_SESSION


def resolve_telethon_session(account: Any) -> Tuple[Any, str, Optional[str]]:
    """
    Build a Telethon session object for this account.

    Returns:
        (session, session_kind, error_code)
        error_code is None on success.
        session_kind: file | string | empty | unknown
    """
    ss = (getattr(account, "session_string", None) or "").strip()
    session_path_attr = getattr(account, "session_path", None)
    sp = (session_path_attr or "").strip() if session_path_attr else ""
    account_id = getattr(account, "id", None)

    paths_to_try: list[Tuple[str, Path]] = []

    if ss:
        raw = _strip_file_url(ss)
        try:
            paths_to_try.append(("session_string", Path(raw).expanduser()))
        except Exception:
            pass

    if sp:
        try:
            paths_to_try.append(("session_path", Path(_strip_file_url(sp)).expanduser().resolve()))
        except Exception:
            pass

    if account_id is not None:
        try:
            from src.core.session_paths import get_canonical_session_path

            paths_to_try.append(("canonical", get_canonical_session_path(int(account_id))))
        except Exception:
            pass

    seen: set[str] = set()
    for _label, path in paths_to_try:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            if path.is_file():
                return _sqlite_session_from_file_path(path), "file", None
        except OSError:
            continue

    if ss:
        raw = _strip_file_url(ss)
        try:
            p = Path(raw).expanduser()
            if _looks_like_filesystem_path(raw) and not p.is_file():
                return None, "file", ERR_SESSION_FILE_MISSING
        except OSError:
            return None, "file", ERR_SESSION_FILE_MISSING

        try:
            return StringSession(ss), "string", None
        except Exception as e:
            logger.info(
                "StringSession rejected",
                account_id=account_id,
                session_kind_guessed="string",
                error=str(e),
            )
            return None, "string", ERR_INVALID_SESSION_FORMAT

    return None, "empty", ERR_EMPTY_SESSION


def human_message_for_code(code: Optional[str]) -> str:
    """Map internal codes to operator-facing delivery / log strings."""
    if not code:
        return ""
    return {
        ERR_EMPTY_SESSION: "No session: empty session_string and no usable session file",
        ERR_SESSION_FILE_MISSING: "Session file missing",
        ERR_INVALID_SESSION_FORMAT: "Invalid session format (not a valid Telethon string session)",
        "unauthorized_session": "Session not authorized with Telegram",
        "failed_connect": "Failed to connect to Telegram",
        "failed_connect_network": "Network or transport error connecting to Telegram (retry)",
        "session_db_locked": "Telegram session database is locked (another process is using this session file — retry shortly)",
        "session_lock_timeout": "Could not acquire exclusive session lock (another worker holds this account — retry)",
        "account_not_found": "Account not found",
    }.get(str(code), str(code) if code else "")
