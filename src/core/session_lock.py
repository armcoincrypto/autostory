"""
Per-account session file lock.
Prevents SQLite "database is locked" when multiple processes open the same
account_<id>.session Telethon session file.
Use: acquire before opening session, release after disconnect.
"""
import fcntl
import os
import time
from pathlib import Path
from typing import Optional, Tuple

import structlog

logger = structlog.get_logger(__name__)

# Default timeout for acquiring lock (seconds)
DEFAULT_LOCK_TIMEOUT_SEC = 30
# Poll interval when waiting
LOCK_POLL_INTERVAL_SEC = 0.5


def _get_locks_dir() -> Path:
    """Locks directory: <data_dir>/locks, e.g. /opt/autostory/data/locks."""
    try:
        from config.settings import settings
        data_dir = Path(settings.storage.sessions_dir).expanduser().resolve().parent
    except Exception:
        data_dir = Path("./data").resolve()
    locks_dir = data_dir / "locks"
    locks_dir.mkdir(parents=True, exist_ok=True)
    return locks_dir


def get_account_lock_path(account_id: int) -> Path:
    """Path for per-account lock file."""
    return _get_locks_dir() / f"account_{account_id}.lock"


class SessionLockHandle:
    """Holds an exclusive file lock. Release via release() or use as context manager."""

    def __init__(self, account_id: int, path: Path, fd: int):
        self.account_id = account_id
        self.path = path
        self._fd = fd

    def release(self) -> None:
        """Release the lock. Safe to call multiple times."""
        if self._fd < 0:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            logger.debug("Session lock released", account_id=self.account_id)
        except Exception as e:
            logger.warning("Session lock release error", account_id=self.account_id, error=str(e))
        finally:
            self._fd = -1

    def __enter__(self) -> "SessionLockHandle":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()


def acquire_session_lock(
    account_id: int,
    timeout_sec: float = DEFAULT_LOCK_TIMEOUT_SEC,
) -> Tuple[bool, Optional[SessionLockHandle], Optional[str]]:
    """
    Acquire exclusive file lock for account session.
    Returns (success, handle_or_none, error_message).
    Caller must call handle.release() when done (e.g. after Telethon disconnect).
    """
    path = get_account_lock_path(account_id)
    start = time.monotonic()
    logger.debug("Session lock acquire attempt", account_id=account_id, path=str(path))

    try:
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as e:
        return False, None, f"Lock file open failed: {e}"

    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                elapsed = time.monotonic() - start
                logger.info(
                    "Session lock acquired",
                    account_id=account_id,
                    wait_sec=round(elapsed, 1),
                )
                return True, SessionLockHandle(account_id, path, fd), None
            except BlockingIOError:
                pass
            elapsed = time.monotonic() - start
            if elapsed >= timeout_sec:
                try:
                    os.close(fd)
                except Exception:
                    pass
                msg = f"Session lock timeout after {timeout_sec}s (another process holds account_{account_id})"
                logger.warning("Session lock timeout", account_id=account_id, timeout_sec=timeout_sec)
                return False, None, msg
            time.sleep(LOCK_POLL_INTERVAL_SEC)
    except Exception as e:
        try:
            os.close(fd)
        except Exception:
            pass
        return False, None, str(e)
