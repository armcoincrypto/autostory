"""
P9.4 — Source-backed per-account session file lock (additive; not wired to production).

File-lock via ``fcntl`` (POSIX). Inspect-only helpers do not acquire locks.
Lock files are never deleted unless ``force=True``.
"""
from __future__ import annotations

import json
import os
import socket
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, Optional

import structlog

logger = structlog.get_logger(__name__)

DEFAULT_LOCKS_DIR = Path("./data/locks")
DEFAULT_STALE_SEC = 3600.0
_META_SUFFIX = ".meta.json"


@dataclass(frozen=True)
class LockInspection:
    account_id: int
    lock_path: str
    meta_path: str
    exists: bool
    held: bool
    stale: bool
    holder_pid: Optional[int]
    holder_host: Optional[str]
    acquired_at: Optional[str]
    warnings: tuple[str, ...]


def get_lock_paths(
    account_id: int,
    *,
    locks_dir: Path | str | None = None,
) -> tuple[Path, Path]:
    base = Path(locks_dir or DEFAULT_LOCKS_DIR).expanduser().resolve()
    aid = int(account_id)
    lock_path = base / f"account_{aid}.lock"
    meta_path = base / f"account_{aid}.lock{_META_SUFFIX}"
    return lock_path, meta_path


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    else:
        return True


def _read_meta(meta_path: Path) -> dict[str, Any]:
    if not meta_path.is_file():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_meta(meta_path: Path, payload: dict[str, Any]) -> None:
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = meta_path.with_suffix(meta_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(tmp, meta_path)


def inspect_lock(
    account_id: int,
    *,
    locks_dir: Path | str | None = None,
    stale_after_sec: float = DEFAULT_STALE_SEC,
) -> LockInspection:
    """
    Inspect lock state without acquiring (opens lock file read-only for probe only).
    """
    lock_path, meta_path = get_lock_paths(account_id, locks_dir=locks_dir)
    warnings: list[str] = []
    exists = lock_path.is_file()
    held = False
    meta = _read_meta(meta_path) if exists else {}
    holder_pid = meta.get("pid")
    holder_host = meta.get("host")
    acquired_at = meta.get("acquired_at")
    pid_int = int(holder_pid) if holder_pid is not None else None

    if exists:
        try:
            fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as e:
            warnings.append(f"open_failed:{type(e).__name__}")
        else:
            try:
                import fcntl

                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    # We got lock — nobody else holds it; release immediately.
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    held = False
                except BlockingIOError:
                    held = True
            finally:
                os.close(fd)

    stale = False
    if exists and not held and meta:
        if pid_int is not None and not _pid_alive(pid_int):
            stale = True
            warnings.append("holder_pid_not_running")
    if exists and stale_after_sec > 0:
        try:
            age = time.time() - lock_path.stat().st_mtime
            if age > stale_after_sec and not held:
                stale = True
                warnings.append("lock_file_mtime_stale")
        except OSError:
            warnings.append("stat_failed")

    return LockInspection(
        account_id=int(account_id),
        lock_path=str(lock_path),
        meta_path=str(meta_path),
        exists=exists,
        held=held,
        stale=stale,
        holder_pid=pid_int,
        holder_host=str(holder_host) if holder_host else None,
        acquired_at=str(acquired_at) if acquired_at else None,
        warnings=tuple(warnings),
    )


@contextmanager
def acquire_session_lock(
    account_id: int,
    *,
    locks_dir: Path | str | None = None,
    timeout_sec: float = 30.0,
    poll_interval_sec: float = 0.25,
    stale_after_sec: float = DEFAULT_STALE_SEC,
    force: bool = False,
) -> Generator[LockInspection, None, None]:
    """
    Acquire exclusive lock for ``account_id``. Yields :class:`LockInspection` after acquire.

    Does not delete the lock file on release unless ``force=True`` (then removes lock + meta).
    """
    import fcntl

    lock_path, meta_path = get_lock_paths(account_id, locks_dir=locks_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    inspection = inspect_lock(
        account_id,
        locks_dir=locks_dir,
        stale_after_sec=stale_after_sec,
    )
    if inspection.stale and inspection.exists and force:
        logger.info(
            "session_lock_v2_force_clear_stale",
            account_id=account_id,
            lock_path=str(lock_path),
        )
        try:
            lock_path.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
        except OSError:
            pass

    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + max(0.0, float(timeout_sec))
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"session_lock_timeout account_id={account_id} path={lock_path}"
                    ) from None
                time.sleep(poll_interval_sec)
        payload = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "acquired_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        _write_meta(meta_path, payload)
        after = inspect_lock(
            account_id,
            locks_dir=locks_dir,
            stale_after_sec=stale_after_sec,
        )
        yield after
    finally:
        if acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)
        if force:
            try:
                lock_path.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)
            except OSError as e:
                logger.warning(
                    "session_lock_v2_force_unlink_failed",
                    account_id=account_id,
                    error=str(e),
                )
