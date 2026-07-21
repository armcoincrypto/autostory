# P9.38 draft restore — source-backed from Cursor snapshots (not byte-matched to archive .pyc).
# Do not restart scheduler until import probe + tests pass and operator approves.

"""
Per-account session file lock.
Prevents SQLite "database is locked" when multiple processes open the same
account_<id>.session Telethon session file.
Use: acquire before opening session, release after disconnect.

P8.10.2 — additive ownership metadata
====================================
``acquire_session_lock`` now accepts optional ``subsystem`` / ``operation``
keyword arguments. When provided, the lock writes a JSON sidecar (
``<lock>.meta``) recording subsystem, pid, started_at, and heartbeat_at. The
sidecar is deleted on release. Callers that don't supply ownership info still
acquire the same flock — no behavioural change. ``inspect_session_lock`` and
``SessionLockState`` surface the sidecar so Dexpert / dashboard can show a
human-readable holder ("readiness_worker pid=527665, held 2.1s").
"""
import fcntl
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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


def get_account_lock_meta_path(account_id: int) -> Path:
    """Sidecar JSON path with ownership metadata. Additive; absent for legacy callers."""
    return _get_locks_dir() / f"account_{account_id}.lock.meta"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_meta(meta_path: Path, payload: Dict[str, Any]) -> None:
    try:
        tmp = meta_path.with_suffix(meta_path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.replace(tmp, meta_path)
    except OSError as exc:
        logger.warning("session_lock_meta_write_failed", path=str(meta_path), error=str(exc))


def _read_meta(meta_path: Path) -> Optional[Dict[str, Any]]:
    if not meta_path.exists():
        return None
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _delete_meta(meta_path: Path) -> None:
    try:
        meta_path.unlink(missing_ok=True)
    except OSError:
        pass


class SessionLockHandle:
    """
    Holds an exclusive file lock. Release via ``release()`` or use as context manager.

    When the holder passed ``subsystem`` / ``operation`` to ``acquire_session_lock``,
    a JSON sidecar with ownership metadata is written next to the lock file and
    deleted on release. ``heartbeat()`` updates ``heartbeat_at`` for long-running ops.
    """

    def __init__(
        self,
        account_id: int,
        path: Path,
        fd: int,
        *,
        meta_path: Optional[Path] = None,
        subsystem: Optional[str] = None,
        operation: Optional[str] = None,
    ):
        self.account_id = account_id
        self.path = path
        self._fd = fd
        self.meta_path = meta_path
        self.subsystem = subsystem
        self.operation = operation

    def heartbeat(self) -> None:
        """Update ``heartbeat_at`` in the sidecar so stale-detection knows we're live."""
        if self.meta_path is None or self._fd < 0:
            return
        meta = _read_meta(self.meta_path) or {}
        meta["heartbeat_at"] = _now_iso()
        meta.setdefault("pid", os.getpid())
        meta.setdefault("subsystem", self.subsystem)
        meta.setdefault("operation", self.operation)
        meta.setdefault("acquired_at", _now_iso())
        _write_meta(self.meta_path, meta)

    def release(self) -> None:
        """Release the lock and clear the sidecar. Safe to call multiple times."""
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
            if self.meta_path is not None:
                _delete_meta(self.meta_path)

    def __enter__(self) -> "SessionLockHandle":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()


def acquire_session_lock(
    account_id: int,
    timeout_sec: float = DEFAULT_LOCK_TIMEOUT_SEC,
    *,
    subsystem: Optional[str] = None,
    operation: Optional[str] = None,
) -> Tuple[bool, Optional[SessionLockHandle], Optional[str]]:
    """
    Acquire exclusive file lock for account session.
    Returns (success, handle_or_none, error_message).
    Caller must call handle.release() when done (e.g. after Telethon disconnect).

    Optional, additive ownership (P8.10.2):
      ``subsystem`` — short identifier (``readiness_worker``, ``scheduler``,
      ``kathleen_listener``, ``ai_loop``, ``dashboard``, ``connect_account``).
      ``operation`` — short verb (``deep_check``, ``connect``, ``send``…).
    When provided, a sidecar ``account_<id>.lock.meta`` JSON file is written
    with ``subsystem``, ``pid``, ``operation``, ``acquired_at``, ``heartbeat_at``
    and is deleted on release. Legacy callers (no kwargs) behave identically to
    pre-P8.10.2 — no metadata is written.
    """
    path = get_account_lock_path(account_id)
    meta_path = get_account_lock_meta_path(account_id)
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
                logger.debug(
                    "Session lock acquired",
                    account_id=account_id,
                    wait_sec=round(elapsed, 1),
                    subsystem=subsystem,
                    operation=operation,
                )
                handle_meta_path: Optional[Path] = None
                if subsystem is not None or operation is not None:
                    payload: Dict[str, Any] = {
                        "account_id": int(account_id),
                        "subsystem": subsystem,
                        "operation": operation,
                        "pid": os.getpid(),
                        "acquired_at": _now_iso(),
                        "heartbeat_at": _now_iso(),
                    }
                    _write_meta(meta_path, payload)
                    handle_meta_path = meta_path
                return (
                    True,
                    SessionLockHandle(
                        account_id,
                        path,
                        fd,
                        meta_path=handle_meta_path,
                        subsystem=subsystem,
                        operation=operation,
                    ),
                    None,
                )
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


# ─────────────────────────────────────────────────────────────────────────────
# Read-only diagnostics (no mutation; safe for Dexpert / dashboard)
# ─────────────────────────────────────────────────────────────────────────────

_ACCOUNT_LOCK_RE = re.compile(r"^account_(\d+)\.lock$")


@dataclass(frozen=True)
class SessionLockState:
    """Snapshot of one account's session-lock file. Inspect-only."""

    account_id: int
    path: Path
    exists: bool
    held: bool
    holder_pids: Tuple[int, ...]
    holder_processes: Tuple[str, ...]
    error: Optional[str] = None

    # P8.10.2 — additive ownership metadata (None when the holder didn't pass it).
    subsystem: Optional[str] = None
    operation: Optional[str] = None
    meta_pid: Optional[int] = None
    acquired_at: Optional[str] = None
    heartbeat_at: Optional[str] = None
    held_for_seconds: Optional[float] = None
    heartbeat_age_seconds: Optional[float] = None
    meta_pid_alive: Optional[bool] = None

    @property
    def stale(self) -> bool:
        """File exists but is not currently held by any live process."""
        if not self.exists:
            return False
        if self.held:
            return False
        if self.holder_pids:
            return False
        # If meta says a PID owns it but that PID is dead, the lock is stale too.
        if self.meta_pid is not None and self.meta_pid_alive is False:
            return True
        return True

    def owner_summary(self) -> str:
        """One-line operator-facing holder description."""
        if self.held:
            base = self.subsystem or "unknown subsystem"
            pid = self.meta_pid or (self.holder_pids[0] if self.holder_pids else None)
            extra = []
            if pid is not None:
                extra.append(f"pid={pid}")
            if self.operation:
                extra.append(f"op={self.operation}")
            if self.held_for_seconds is not None:
                extra.append(f"held_for={self.held_for_seconds:.1f}s")
            return base + (" · " + " · ".join(extra) if extra else "")
        if self.stale:
            return "stale (no live holder)"
        return "free"

    def recommended_action(self) -> str:
        """Conservative next-step guidance. Never suggests deleting active locks."""
        if self.held:
            if self.heartbeat_age_seconds is not None and self.heartbeat_age_seconds > 60:
                return (
                    "Holder advertised a heartbeat but it is stale (>60s). Investigate the "
                    f"{self.subsystem or 'unknown'} worker (pid={self.meta_pid}); do not "
                    "delete the lock while a process holds the flock."
                )
            return "Wait for current holder to release; this is expected during connect/probe."
        if self.stale:
            return (
                "Lock file is empty and not held by any live process. Safe to ignore; "
                "operator may delete the empty lock file manually if it is from a crashed worker."
            )
        return "No action needed."


def _holder_pids_via_proc(path: Path) -> Tuple[int, ...]:
    """
    Best-effort: walk /proc/*/fd to find any PID that has ``path`` open.

    Returns empty tuple on platforms without ``/proc`` or on permission errors.
    """
    pids: List[int] = []
    proc = Path("/proc")
    if not proc.is_dir():
        return ()
    target = str(path.resolve())
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        fd_dir = entry / "fd"
        try:
            for fd in fd_dir.iterdir():
                try:
                    if str(os.readlink(fd)) == target:
                        pids.append(int(entry.name))
                        break
                except OSError:
                    continue
        except (OSError, PermissionError):
            continue
    return tuple(sorted(set(pids)))


def _process_descriptor(pid: int) -> str:
    """``cmdline`` (truncated) or ``comm`` for a PID; never raises."""
    try:
        cmd = (Path(f"/proc/{pid}/cmdline").read_bytes() or b"").replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    except Exception:
        cmd = ""
    if cmd:
        return cmd[:120]
    try:
        return Path(f"/proc/{pid}/comm").read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return f"pid={pid}"


def _pid_alive(pid: int) -> bool:
    """Cheap liveness probe; returns False on permission errors only when truly dead."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _iso_to_epoch(iso: str) -> Optional[float]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


def inspect_session_lock(account_id: int) -> SessionLockState:
    """
    Read-only snapshot of one account's session lock.

    Combines a non-blocking ``flock(LOCK_EX|LOCK_NB)`` probe (released immediately),
    a ``/proc/*/fd`` holder scan, and (P8.10.2) sidecar ownership metadata. **No mutation.**
    """
    path = get_account_lock_path(int(account_id))
    meta_path = get_account_lock_meta_path(int(account_id))

    if not path.exists():
        return SessionLockState(
            account_id=int(account_id),
            path=path,
            exists=False,
            held=False,
            holder_pids=(),
            holder_processes=(),
        )

    pids = _holder_pids_via_proc(path)
    procs = tuple(_process_descriptor(p) for p in pids)

    held = False
    err: Optional[str] = None
    fd = -1
    try:
        fd = os.open(str(path), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except Exception:
                pass
            held = False
        except BlockingIOError:
            held = True
    except OSError as exc:
        err = f"open failed: {exc}"
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except Exception:
                pass

    meta = _read_meta(meta_path) or {}
    subsystem = meta.get("subsystem") if isinstance(meta, dict) else None
    operation = meta.get("operation") if isinstance(meta, dict) else None
    meta_pid_raw = meta.get("pid") if isinstance(meta, dict) else None
    meta_pid = int(meta_pid_raw) if isinstance(meta_pid_raw, int) else None
    acquired_at = meta.get("acquired_at") if isinstance(meta, dict) else None
    heartbeat_at = meta.get("heartbeat_at") if isinstance(meta, dict) else None

    now_epoch = time.time()
    held_for: Optional[float] = None
    if isinstance(acquired_at, str):
        e = _iso_to_epoch(acquired_at)
        if e is not None:
            held_for = max(0.0, now_epoch - e)

    heartbeat_age: Optional[float] = None
    if isinstance(heartbeat_at, str):
        e = _iso_to_epoch(heartbeat_at)
        if e is not None:
            heartbeat_age = max(0.0, now_epoch - e)

    pid_alive: Optional[bool] = None
    if meta_pid is not None:
        pid_alive = _pid_alive(meta_pid)

    return SessionLockState(
        account_id=int(account_id),
        path=path,
        exists=True,
        held=held,
        holder_pids=pids,
        holder_processes=procs,
        error=err,
        subsystem=subsystem if isinstance(subsystem, str) else None,
        operation=operation if isinstance(operation, str) else None,
        meta_pid=meta_pid,
        acquired_at=acquired_at if isinstance(acquired_at, str) else None,
        heartbeat_at=heartbeat_at if isinstance(heartbeat_at, str) else None,
        held_for_seconds=held_for,
        heartbeat_age_seconds=heartbeat_age,
        meta_pid_alive=pid_alive,
    )


def list_known_lock_account_ids() -> Tuple[int, ...]:
    """Account ids that have a lock file on disk. Read-only."""
    locks_dir = _get_locks_dir()
    out: List[int] = []
    try:
        for child in locks_dir.iterdir():
            m = _ACCOUNT_LOCK_RE.match(child.name)
            if m:
                out.append(int(m.group(1)))
    except OSError:
        return ()
    return tuple(sorted(out))
