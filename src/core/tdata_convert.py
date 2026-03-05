"""
Convert Telegram Desktop tdata folder to Telethon session string.
Used by Dashboard (upload zip) and by scripts/convert_tdata.py CLI.
"""
import base64
import ipaddress
import logging
import os
import re
import struct
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    from opentele.td import TDesktop
    from opentele.api import UseCurrentSession, API, APIData
except ImportError:
    TDesktop = None
    UseCurrentSession = None
    API = None
    APIData = None

from telethon.sessions import StringSession
from telethon.sessions.sqlite import SQLiteSession

# Same format as StringSession.save() for session_file_to_string
_SESSION_STRUCT_PREFORMAT = ">B{}sH256s"
_SESSION_VERSION = "1"


def _session_to_string(session) -> str:
    """Build a session string from a session object that has dc_id, server_address, port, auth_key."""
    if not getattr(session, "auth_key", None) or not session.auth_key:
        return ""
    ip = ipaddress.ip_address(session.server_address).packed
    data = struct.pack(
        _SESSION_STRUCT_PREFORMAT.format(len(ip)),
        session.dc_id,
        ip,
        session.port,
        session.auth_key.key,
    )
    return _SESSION_VERSION + base64.urlsafe_b64encode(data).decode("ascii")


def _has_map_json(d: Path) -> bool:
    """True if this directory contains map.json (case-insensitive)."""
    if not d.is_dir():
        return False
    for f in d.iterdir():
        if f.is_file() and f.name.lower() == "map.json":
            return True
    return False


def session_file_to_string(session_path: str | Path) -> str:
    """
    Read a Telethon .session file (SQLite) and return the equivalent session string.
    Supports session/12345.session or session/12345 (no extension).
    """
    path = Path(session_path).resolve()
    if not path.exists():
        path = Path(str(path) + ".session") if not str(path).endswith(".session") else path
        if not path.exists():
            raise FileNotFoundError(f"Not found: {path}")
    elif path.suffix.lower() != ".session":
        # File exists with no extension (e.g. session/12345) - use as-is
        pass
    session = SQLiteSession(str(path))
    if not session.auth_key:
        raise ValueError(f"No auth key in session file: {path}")
    ip = ipaddress.ip_address(session.server_address).packed
    data = struct.pack(
        _SESSION_STRUCT_PREFORMAT.format(len(ip)),
        session.dc_id,
        ip,
        session.port,
        session.auth_key.key,
    )
    return _SESSION_VERSION + base64.urlsafe_b64encode(data).decode("ascii")


def find_tdata_root(extracted_dir: Path) -> Path:
    """
    Find the tdata folder inside an extracted directory.
    Zip might contain: tdata/map.json, or nested layout like session/tdata, portable/..., etc.
    """
    extracted_dir = Path(extracted_dir).resolve()
    if not extracted_dir.is_dir():
        raise ValueError("Not a directory")

    # Direct tdata folder (extracted_dir/tdata)
    tdata_sub = extracted_dir / "tdata"
    if tdata_sub.is_dir():
        if _has_map_json(tdata_sub):
            return tdata_sub
        for f in tdata_sub.rglob("*"):
            if f.is_file() and f.name.lower() == "map.json":
                return f.parent
        return tdata_sub
    # Case-insensitive: any direct child dir named tdata (e.g. Tdata, TDATA)
    for p in extracted_dir.iterdir():
        if p.is_dir() and p.name.lower() == "tdata":
            if _has_map_json(p):
                return p
            for f in p.rglob("*"):
                if f.is_file() and f.name.lower() == "map.json":
                    return f.parent
            return p
    # Contents at root (map.json in extracted dir)
    if _has_map_json(extracted_dir):
        return extracted_dir
    # Single subdirectory (e.g. 50-us-27.01/map.json or account_folder/tdata)
    subs = [p for p in extracted_dir.iterdir() if p.is_dir()]
    if len(subs) == 1:
        one = subs[0]
        if one.name.lower() == "tdata":
            if _has_map_json(one):
                return one
            for f in one.rglob("*"):
                if f.is_file() and f.name.lower() == "map.json":
                    return f.parent
            return one
        if _has_map_json(one):
            return one
        nested_tdata = one / "tdata"
        if nested_tdata.is_dir():
            if _has_map_json(nested_tdata):
                return nested_tdata
            for f in nested_tdata.rglob("*"):
                if f.is_file() and f.name.lower() == "map.json":
                    return f.parent
            return nested_tdata
        for p2 in one.iterdir():
            if p2.is_dir() and p2.name.lower() == "tdata":
                if _has_map_json(p2):
                    return p2
                for f in p2.rglob("*"):
                    if f.is_file() and f.name.lower() == "map.json":
                        return f.parent
                return p2
    # Search any subdir for map.json or dir named tdata (multiple top-level items)
    for d in subs:
        if d.name.lower() == "tdata":
            if _has_map_json(d):
                return d
            for f in d.rglob("*"):
                if f.is_file() and f.name.lower() == "map.json":
                    return f.parent
            return d
        if _has_map_json(d):
            return d
        nested = d / "tdata"
        if nested.is_dir():
            if _has_map_json(nested):
                return nested
            for f in nested.rglob("*"):
                if f.is_file() and f.name.lower() == "map.json":
                    return f.parent
            return nested
        for p2 in d.iterdir():
            if p2.is_dir() and p2.name.lower() == "tdata":
                if _has_map_json(p2):
                    return p2
                for f in p2.rglob("*"):
                    if f.is_file() and f.name.lower() == "map.json":
                        return f.parent
                return p2
        for sub2 in d.iterdir():
            if sub2.is_dir() and _has_map_json(sub2):
                return sub2
    # Recursive search: e.g. session/tdata, portable/Telegram Desktop/tdata, session/xxx/tdata
    max_depth = 8
    def search_recursive(parent: Path, depth: int) -> Path | None:
        if depth > max_depth:
            return None
        for p in parent.iterdir():
            if not p.is_dir():
                continue
            if _has_map_json(p):
                return p
            found = search_recursive(p, depth + 1)
            if found is not None:
                return found
        return None
    found = search_recursive(extracted_dir, 0)
    if found is not None:
        return found
    # Fallback: any file named map.json (any depth, case-insensitive) — use its parent as tdata root
    for f in extracted_dir.rglob("*"):
        if f.is_file() and f.name.lower() == "map.json":
            return f.parent
    # Helpful error: show what we found
    top = list(extracted_dir.iterdir())
    top_names = [p.name for p in top[:20]]
    raise ValueError(
        "No tdata folder found in upload. The zip must contain a folder with map.json "
        "(e.g. tdata from Telegram Desktop, or a folder named 'tdata' inside session/portable). "
        f"Top-level contents: {top_names}"
    )


# Telethon session strings: version 1 then base64 (starts with 1A, 1B, etc.), at least 80 chars
_SESSION_STRING_RE = re.compile(r"1[A-Za-z0-9+/=]{80,}")


def find_session_string_in_extracted(extracted_dir: Path) -> str | None:
    """
    Search extracted zip for any text file containing a Telethon session string.
    Returns the first match, or None.
    """
    extracted_dir = Path(extracted_dir).resolve()
    if not extracted_dir.is_dir():
        return None

    def search_text(text: str) -> str | None:
        # Try line by line first
        for line in text.splitlines():
            line = line.strip()
            m = _SESSION_STRING_RE.search(line)
            if m:
                return m.group(0)
        # Then try whole text (string might be wrapped or have spaces)
        compact = re.sub(r"\s+", "", text)
        m = _SESSION_STRING_RE.search(compact)
        if m:
            return m.group(0)
        return None

    # Search by extension, then any small text-like file
    exts = (".txt", ".session", ".key", ".dat", ".json")
    for ext in exts:
        for f in extracted_dir.rglob("*" + ext):
            if not f.is_file() or f.suffix.lower() != ext:
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
                found = search_text(text)
                if found:
                    return found
            except Exception:
                continue

    # No extension or other names: files under 100KB that look like text
    for f in extracted_dir.rglob("*"):
        if not f.is_file() or f.suffix.lower() in (".zip", ".exe", ".dll", ".so", ".pyc"):
            continue
        if f.stat().st_size > 100_000:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
            if "\x00" in text[:1000]:  # skip binary
                continue
            found = search_text(text)
            if found:
                return found
        except Exception:
            continue

    # Fallback: .session files are SQLite; convert to session string (e.g. zip with session/12345.session)
    for f in sorted(extracted_dir.rglob("*.session")):
        if not f.is_file() or f.suffix.lower() != ".session":
            continue
        try:
            return session_file_to_string(f)
        except Exception:
            continue
    return None


def find_all_session_strings_in_extracted(extracted_dir: Path) -> list[str]:
    """
    Collect all session strings from an extracted zip: from text files and from .session files.
    Returns a deduplicated list (order: text matches first, then .session conversions).
    Use when the zip has many accounts (e.g. 50 .session files).
    """
    extracted_dir = Path(extracted_dir).resolve()
    if not extracted_dir.is_dir():
        return []

    seen: set[str] = set()
    result: list[str] = []

    def add(s: str) -> None:
        s = s.strip()
        if s and s not in seen and len(s) >= 90:
            seen.add(s)
            result.append(s)

    def search_text(text: str) -> None:
        for line in text.splitlines():
            line = line.strip()
            m = _SESSION_STRING_RE.search(line)
            if m:
                add(m.group(0))
        compact = re.sub(r"\s+", "", text)
        for m in _SESSION_STRING_RE.finditer(compact):
            add(m.group(0))

    # Text files: collect all matches
    exts = (".txt", ".key", ".dat", ".json")
    for ext in exts:
        for f in extracted_dir.rglob("*" + ext):
            if not f.is_file() or f.suffix.lower() != ext:
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
                search_text(text)
            except Exception:
                continue
    for f in extracted_dir.rglob("*"):
        if not f.is_file() or f.suffix.lower() in (".zip", ".exe", ".dll", ".so", ".pyc", ".session"):
            continue
        if f.stat().st_size > 100_000:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
            if "\x00" in text[:1000]:
                continue
            search_text(text)
        except Exception:
            continue

    # All .session files
    session_files = sorted(extracted_dir.rglob("*.session"))
    converted = 0
    for f in session_files:
        if not f.is_file() or f.suffix.lower() != ".session":
            continue
        try:
            add(session_file_to_string(f))
            converted += 1
        except Exception:
            continue

    # Fallback: files inside a "session" folder with no extension (e.g. session/12345)
    session_dirs = [p for p in extracted_dir.rglob("session") if p.is_dir()] or []
    for session_dir in session_dirs:
        for f in sorted(session_dir.iterdir()):
            if not f.is_file() or f.suffix.lower() == ".session":
                continue
            if f.suffix.lower() in (".zip", ".json", ".txt", ".key"):
                continue
            try:
                add(session_file_to_string(f))
                converted += 1
            except Exception:
                continue

    if session_files or converted:
        logger.info(
            "find_all_session_strings: dir=%s .session_files=%d converted=%d unique_strings=%d",
            extracted_dir.name,
            len(session_files),
            converted,
            len(result),
        )
    return result


async def tdata_to_session_string(tdata_path: str | Path, passcode: str | None = None) -> str:
    """
    Convert a tdata directory to a Telethon session string.
    :param tdata_path: Path to the tdata folder (must contain map.json, etc.)
    :param passcode: Optional local passcode if Telegram Desktop uses one
    :return: Session string
    :raises ValueError: On invalid path or conversion failure
    """
    if TDesktop is None:
        raise ValueError("opentele is not installed. Run: pip install opentele")

    path = Path(tdata_path).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"Not a directory: {path}")

    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    if api_id and api_hash:
        api = APIData(int(api_id), api_hash)
    else:
        api = API.TelegramDesktop

    key_file_options = [None, "datas"]
    tdesk = None
    last_err = None
    for key_file in key_file_options:
        try:
            if key_file is None:
                tdesk = TDesktop(str(path), api=api, passcode=passcode)
            else:
                tdesk = TDesktop(str(path), api=api, passcode=passcode, keyFile=key_file)
            break
        except Exception as e:
            err = str(e)
            last_err = e
            if "key_data" in err or "TFileNotFound" in err or "Could not open" in err:
                continue
            raise
    if tdesk is None:
        raise ValueError(
            last_err if last_err else "Could not open tdata (tried key_data and key_datas). "
            "Ensure the folder is a full tdata export from Telegram Desktop or a compatible export."
        )

    if not tdesk.isLoaded():
        raise ValueError("No authorized account in tdata. Log in to Telegram Desktop first.")

    # Pass session=None: opentele has a bug where session=StringSession() leaves auth_session unset (UnboundLocalError).
    # With None, it uses SQLiteSession(None) or MemorySession; we then save() to get the session string.
    client = await tdesk.ToTelethon(
        session=None,
        flag=UseCurrentSession,
        api=api,
    )
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise ValueError("Session not authorized.")
        # session=None makes opentele use SQLiteSession/MemorySession; their save() doesn't return a string.
        session_string = client.session.save()
        if not session_string and getattr(client.session, "auth_key", None):
            session_string = _session_to_string(client.session)
        if not session_string:
            raise ValueError("Could not export session string from tdata.")
        return session_string
    finally:
        await client.disconnect()
