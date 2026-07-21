"""
Cross-process hint: account has an in-flight operator action (e.g. Send Test).

Web and readiness worker are different processes — a plain Python set is not shared.
We mirror active account ids with tiny marker files under ``data/runtime/active_accounts/``
so the readiness worker can skip Telethon work and avoid session lock fights.

In-process callers also update ``active_accounts`` under a threading.Lock for consistency.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Set

# Stale markers (e.g. web worker killed mid-request) are ignored and removed.
MARKER_MAX_AGE_SEC = float(os.environ.get("AUTOSTORY_ACTIVE_ACCOUNT_MAX_AGE_SEC", "600"))

_lock = threading.Lock()
active_accounts: Set[int] = set()


def _runtime_dir() -> Path:
    try:
        from config.settings import settings

        base = Path(settings.storage.sessions_dir).expanduser().resolve().parent
    except Exception:
        base = Path("./data").resolve()
    d = base / "runtime" / "active_accounts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _marker_path(account_id: int) -> Path:
    return _runtime_dir() / f"account_{int(account_id)}.active"


def mark_account_active(account_id: int) -> None:
    aid = int(account_id)
    p = _marker_path(aid)
    line = f"{os.getpid()}\n{time.time()}\n"
    tmp = p.with_suffix(".active.tmp")
    with _lock:
        tmp.write_text(line, encoding="ascii")
        tmp.replace(p)
        active_accounts.add(aid)


def unmark_account_active(account_id: int) -> None:
    aid = int(account_id)
    p = _marker_path(aid)
    with _lock:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
        active_accounts.discard(aid)


def is_account_active(account_id: int) -> bool:
    """True if another process marked this account active recently."""
    aid = int(account_id)
    p = _marker_path(aid)
    with _lock:
        if not p.is_file():
            active_accounts.discard(aid)
            return False
        try:
            age = time.time() - p.stat().st_mtime
        except OSError:
            return False
        if age > MARKER_MAX_AGE_SEC:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
            active_accounts.discard(aid)
            return False
        active_accounts.add(aid)
        return True
