"""
Background polling for AI Agent auto_mode tasks (supervised / autonomous).

Disabled by default (AI_AGENT_AUTO_LOOP_ENABLED=false). One daemon thread per process.
Each cycle queries task ids with auto_mode != 'off' and runs AiAgentAutoLoop.process_task
in its own DB transaction. Failures are logged; they never crash the worker loop.

Fairness: newest active tasks (by last_activity_at) are processed first so a stuck
old “retrying” task does not starve a new one. Per-account 60s cooldown after a
session lock / busy result so other accounts can proceed.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
import traceback
from datetime import datetime, timedelta
from typing import Optional

import structlog
from sqlalchemy import desc
from sqlalchemy.exc import OperationalError as SAOperationalError

from src.telegram_gateway import errors as gw_errors

from src.ai_agent.account_allowlist import resolve_ai_agent_task_permitted_account_ids
from src.ai_agent.auto_loop import AiAgentAutoLoop
from src.core.ai_agent_models import AiAgentTask
from src.core.database import get_db_context

logger = structlog.get_logger(__name__)

_started = False
_lock = threading.Lock()
_autoloop_flock_fd: Optional[int] = None

_ACCOUNT_COOLDOWN_UNTIL: dict[int, datetime] = {}
ACCOUNT_LOCK_COOLDOWN_SEC = 60.0


def _purge_stale_account_cooldowns() -> None:
    now = datetime.utcnow()
    expired = [aid for aid, until in _ACCOUNT_COOLDOWN_UNTIL.items() if until <= now]
    for aid in expired:
        del _ACCOUNT_COOLDOWN_UNTIL[aid]


def _account_in_cooldown(account_id: int) -> bool:
    _purge_stale_account_cooldowns()
    until = _ACCOUNT_COOLDOWN_UNTIL.get(int(account_id))
    return until is not None and datetime.utcnow() < until


def _note_account_session_busy(account_id: int) -> None:
    _ACCOUNT_COOLDOWN_UNTIL[int(account_id)] = datetime.utcnow() + timedelta(
        seconds=ACCOUNT_LOCK_COOLDOWN_SEC
    )


def _result_implies_account_lock_cooldown(res: dict) -> bool:
    """Cooldown same account so other tasks/accounts can run (fairness)."""
    if not isinstance(res, dict):
        return False
    if res.get("session_lock"):
        return True
    ec = str(res.get("error_code") or "").strip().lower()
    if ec == "session_lock_timeout" or "session_lock" in ec or ec == "session_db_locked":
        return True
    if ec == gw_errors.GATEWAY_TIMEOUT:
        return True
    return False


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return float(default)
    try:
        return float(str(raw).strip())
    except ValueError:
        return float(default)


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        v = int(default)
    else:
        try:
            v = int(str(raw).strip())
        except ValueError:
            v = int(default)
    return max(lo, min(v, hi))


def _sqlite_locked(exc: Exception) -> bool:
    return "database is locked" in str(exc).lower()


def _run_one_cycle(loop: AiAgentAutoLoop) -> None:
    max_tasks = _env_int("AI_AGENT_AUTO_LOOP_MAX_TASKS", 3, 1, 50)
    try:
        with get_db_context() as db:
            permitted = resolve_ai_agent_task_permitted_account_ids(db)
            rows = (
                db.query(AiAgentTask.id, AiAgentTask.account_id)
                .filter(AiAgentTask.auto_mode != "off")
                .filter(AiAgentTask.account_id.in_(tuple(sorted(permitted))))
                .filter(
                    ~AiAgentTask.status.in_(
                        ("paused", "completed", "failed", "cancelled")
                    )
                )
                .order_by(
                    desc(AiAgentTask.last_activity_at),
                    desc(AiAgentTask.id),
                )
                .all()
            )
    except (sqlite3.OperationalError, SAOperationalError) as e:
        if _sqlite_locked(e):
            logger.warning("ai_agent_auto_loop_db_busy", phase="list", error=str(e))
        else:
            logger.warning("ai_agent_auto_loop_list_failed", error=str(e))
        return
    except Exception as e:
        logger.warning("ai_agent_auto_loop_list_failed", error=str(e))
        return

    rows = rows[:max_tasks]

    for tid, account_id in rows:
        if _account_in_cooldown(account_id):
            continue
        try:
            with get_db_context() as db:
                res = loop.process_task(db, int(tid), ignore_delay=False)
            if isinstance(res, dict) and _result_implies_account_lock_cooldown(res):
                _note_account_session_busy(int(account_id))
        except (sqlite3.OperationalError, SAOperationalError) as e:
            if _sqlite_locked(e):
                logger.warning(
                    "ai_agent_auto_loop_db_busy",
                    phase="process_task",
                    task_id=tid,
                    error=str(e),
                )
            else:
                logger.warning(
                    "ai_agent_auto_loop_task_failed",
                    task_id=tid,
                    error=str(e),
                    traceback=traceback.format_exc(),
                )
        except Exception as e:
            logger.warning(
                "ai_agent_auto_loop_task_failed",
                task_id=tid,
                error=str(e),
                traceback=traceback.format_exc(),
            )


def _thread_main() -> None:
    loop = AiAgentAutoLoop()
    sleep_sec = _env_float("AI_AGENT_AUTO_LOOP_CYCLE_SEC", 15.0)
    sleep_sec = max(5.0, min(sleep_sec, 120.0))
    logger.info(
        "ai_agent_auto_loop_thread_running",
        cycle_sleep_sec=sleep_sec,
        pid=os.getpid(),
    )
    while True:
        try:
            _run_one_cycle(loop)
        except Exception as e:
            logger.warning(
                "ai_agent_auto_loop_cycle_error",
                error=str(e),
                traceback=traceback.format_exc(),
            )
        time.sleep(sleep_sec)


def _try_acquire_autoloop_flock() -> bool:
    """
    Non-blocking flock so only one process runs the AI auto-loop (extra safety with gunicorn -w 1).
    Disabled with AI_AGENT_AUTO_LOOP_FLOCK=false (e.g. some test environments).
    """
    global _autoloop_flock_fd
    try:
        import fcntl
    except ImportError:
        return True

    path = os.environ.get(
        "AI_AGENT_AUTO_LOOP_LOCK_PATH",
        "/tmp/storyfleet_ai_agent_autoloop.lock",
    )
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _autoloop_flock_fd = fd
        return True
    except BlockingIOError:
        logger.warning(
            "ai_agent_auto_loop_flock_held_elsewhere",
            lock_path=path,
            pid=os.getpid(),
        )
        return False
    except OSError as e:
        logger.warning(
            "ai_agent_auto_loop_flock_failed", lock_path=path, error=str(e)
        )
        return False


def start_ai_agent_auto_loop_background() -> None:
    """Start daemon thread when AI_AGENT_AUTO_LOOP_ENABLED is truthy."""
    global _started
    raw = os.environ.get("AI_AGENT_AUTO_LOOP_ENABLED", "false").strip().lower()
    enabled = raw in ("1", "true", "yes", "on")
    if not enabled:
        logger.info("ai_agent_auto_loop_skipped_disabled", enabled=raw, pid=os.getpid())
        return

    flock_raw = os.environ.get("AI_AGENT_AUTO_LOOP_FLOCK", "true").strip().lower()
    if flock_raw not in ("0", "false", "no", "off"):
        if not _try_acquire_autoloop_flock():
            return

    with _lock:
        if _started:
            return
        _started = True

    logger.info("ai_agent_auto_loop_background_start", pid=os.getpid())
    t = threading.Thread(
        target=_thread_main,
        name="ai-agent-auto-loop",
        daemon=True,
    )
    t.start()
