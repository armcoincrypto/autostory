"""
P6.2 — Readiness worker policy: config validation, account selection, runtime status.

Read-only helpers for tests and operator observability. Probe execution remains in
``readiness_worker._deep_check_one``.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.orm import Session

from src.ai_agent.account_allowlist import account_id_excluded_from_readiness_worker_core
from src.clients import readiness_store
from src.clients.session_resolve import probe_telethon_session_kind
from src.core.account_operational_state import compute_account_operational_state
from src.core.account_runtime_state import is_account_active
from src.core.models import Account

STATUS_FILE = Path(
    os.environ.get(
        "READINESS_WORKER_STATUS_FILE",
        "/opt/autostory/data/runtime/readiness_worker_status.json",
    )
)


def _env_int(name: str, default: int, *, minimum: int = 1, maximum: int = 86400) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return int(default)
    try:
        return max(minimum, min(int(raw), maximum))
    except ValueError:
        return int(default)


def _env_float(name: str, default: float, *, minimum: float = 0.1, maximum: float = 86400.0) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return float(default)
    try:
        return max(minimum, min(float(raw), maximum))
    except ValueError:
        return float(default)


@dataclass(frozen=True)
class ReadinessWorkerConfig:
    enabled: bool
    dry_run: bool
    max_parallel: int
    per_account_timeout_sec: float
    cycle_sleep_sec: float
    account_delay_sec: float
    session_lock_acquire_sec: float
    batch_size: int
    auth_failure_backoff_sec: int
    transient_backoff_sec: int
    allow_ids: Optional[frozenset[int]]

    @classmethod
    def from_environ(cls) -> "ReadinessWorkerConfig":
        enabled_raw = (os.environ.get("READINESS_WORKER_ENABLED") or "true").strip().lower()
        dry_raw = (os.environ.get("READINESS_WORKER_DRY_RUN") or "false").strip().lower()
        allow_raw = (os.environ.get("READINESS_WORKER_ALLOW_IDS") or "").strip()
        allow_ids: Optional[frozenset[int]] = None
        if allow_raw:
            allow_ids = frozenset(int(x.strip()) for x in allow_raw.split(",") if x.strip())
        per_account_timeout = _env_float("READINESS_WORKER_PER_ACCOUNT_TIMEOUT_SEC", 20.0)
        session_lock_raw = _env_float("READINESS_WORKER_SESSION_LOCK_TIMEOUT_SEC", 12.0)
        session_lock_acquire = min(
            max(3.0, session_lock_raw),
            max(4.0, per_account_timeout - 1.0),
        )
        return cls(
            enabled=enabled_raw in ("1", "true", "yes", "on"),
            dry_run=dry_raw in ("1", "true", "yes", "on"),
            max_parallel=_env_int("READINESS_WORKER_MAX_PARALLEL", 1, minimum=1, maximum=5),
            per_account_timeout_sec=per_account_timeout,
            cycle_sleep_sec=_env_float("READINESS_WORKER_CYCLE_SEC", 60.0),
            account_delay_sec=_env_float("READINESS_WORKER_ACCOUNT_DELAY_SEC", 0.3),
            session_lock_acquire_sec=session_lock_acquire,
            batch_size=_env_int("READINESS_WORKER_BATCH_SIZE", 15, minimum=1, maximum=200),
            auth_failure_backoff_sec=_env_int(
                "READINESS_WORKER_AUTH_FAILURE_BACKOFF_SEC", 3600, minimum=60, maximum=86400
            ),
            transient_backoff_sec=_env_int(
                "READINESS_WORKER_TRANSIENT_BACKOFF_SEC", 300, minimum=30, maximum=3600
            ),
            allow_ids=allow_ids,
        )

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.per_account_timeout_sec <= self.session_lock_acquire_sec:
            errors.append("READINESS_WORKER_PER_ACCOUNT_TIMEOUT_SEC must exceed session lock timeout")
        if self.batch_size < 1:
            errors.append("READINESS_WORKER_BATCH_SIZE must be >= 1")
        return errors


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _checked_age_sec(snap: readiness_store.ReadinessSnapshot, now: datetime) -> Optional[float]:
    ca = getattr(snap, "checked_at", None)
    if ca is None or not isinstance(ca, datetime):
        return None
    return (now - ca).total_seconds()


def account_probe_due(
    db: Session,
    account: Account,
    *,
    now: Optional[datetime] = None,
    config: Optional[ReadinessWorkerConfig] = None,
) -> tuple[bool, str]:
    """
    Return (due, skip_reason). Uses canonical readiness snapshots and backoff policy.
    """
    cfg = config or ReadinessWorkerConfig.from_environ()
    aid = int(account.id)
    now = now or _utc_now_naive()

    if cfg.allow_ids is not None and aid not in cfg.allow_ids:
        return False, "allow_filter"
    if account_id_excluded_from_readiness_worker_core(aid):
        return False, "excluded_ai_reserved"
    purpose = (getattr(account, "purpose", None) or "both").strip().lower()
    if purpose in ("autostory", "ai_agent"):
        return False, "excluded_purpose"

    status_val = getattr(account.status, "value", str(account.status or "")).lower()
    if status_val != "active":
        return False, "disabled"

    op = compute_account_operational_state(db, account)
    tier = (op.get("tier") or "").lower()
    if tier in ("reserved", "controller"):
        return False, "quarantined"

    kind, err = probe_telethon_session_kind(account)
    session_exists = err is None and kind in ("file", "string")
    if err is not None:
        return True, "resolver_error_due"
    if not session_exists:
        return False, "no_session_material"

    if is_account_active(aid):
        return False, "active_runtime"

    snap = readiness_store.fetch_snapshot(db, aid)
    if snap is None:
        return True, "missing_snapshot"

    if not readiness_store.snapshot_row_valid(snap, now):
        return True, "expired_snapshot"

    status = (snap.status or "").strip().upper()
    age = _checked_age_sec(snap, now)

    if status == readiness_store.STAT_READY and age is not None:
        if age <= float(readiness_store.READINESS_TRUST_WINDOW_SEC):
            return False, "fresh_ready"
        return True, "stale_ready"

    if status == readiness_store.STAT_NOT_AUTH and age is not None:
        if age < float(cfg.auth_failure_backoff_sec):
            return False, "auth_failure_backoff"
        return True, "auth_backoff_elapsed"

    if status == readiness_store.STAT_TEMP and age is not None:
        if age < float(cfg.transient_backoff_sec):
            return False, "transient_backoff"
        return True, "transient_backoff_elapsed"

    if status == readiness_store.STAT_ERROR and age is not None:
        if age < float(readiness_store.ERROR_TTL_SEC):
            return False, "error_backoff"
        return True, "error_backoff_elapsed"

    if age is not None and age <= float(readiness_store.READINESS_TRUST_WINDOW_SEC):
        return False, "trusted_recent"

    return True, "due"


def select_probe_candidates(
    db: Session,
    accounts: list[Account],
    *,
    config: Optional[ReadinessWorkerConfig] = None,
    now: Optional[datetime] = None,
) -> tuple[list[int], dict[str, int]]:
    """Select account IDs for the next worker cycle (bounded by batch_size)."""
    cfg = config or ReadinessWorkerConfig.from_environ()
    now = now or _utc_now_naive()
    skip_counts: dict[str, int] = {}
    candidates: list[tuple[tuple, int]] = []

    for acc in accounts:
        due, reason = account_probe_due(db, acc, now=now, config=cfg)
        if not due:
            skip_counts[reason] = skip_counts.get(reason, 0) + 1
            continue
        aid = int(acc.id)
        snap = readiness_store.fetch_snapshot(db, aid)
        if snap is None:
            priority = (0, datetime.min, aid)
        else:
            ca = getattr(snap, "checked_at", None)
            if isinstance(ca, datetime):
                priority = (0, ca, aid)
            else:
                priority = (0, datetime.min, aid)
        candidates.append((priority, aid))

    candidates.sort(key=lambda x: x[0])
    ids = [aid for _, aid in candidates[: cfg.batch_size]]
    return ids, skip_counts


def write_runtime_status(payload: dict[str, Any]) -> None:
    """Persist last cycle summary for operator system-safety (best-effort)."""
    try:
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATUS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(STATUS_FILE)
    except Exception:
        pass


def read_runtime_status() -> dict[str, Any]:
    try:
        if STATUS_FILE.is_file():
            return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}
