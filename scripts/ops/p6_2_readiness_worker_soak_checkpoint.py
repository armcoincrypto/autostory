#!/usr/bin/env python3
"""
P6.2A — Read-only readiness worker soak checkpoint.

Collects production safety evidence and compares against an immutable baseline.
Exit codes: 0 healthy, 1 warning, 2 blocker.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

AUDIT_DIR = Path(os.environ.get("P6_2_SOAK_AUDIT_DIR", str(_REPO / "data" / "audit" / "p6_2_soak_checkpoints")))
BASELINE_GLOB = "p6_2_soak_baseline_*.json"
STATUS_FILE = Path(
    os.environ.get(
        "READINESS_WORKER_STATUS_FILE",
        str(_REPO / "data" / "runtime" / "readiness_worker_status.json"),
    )
)
DB_PATH = Path(os.environ.get("STORYFLEET_DB_PATH", str(_REPO / "data" / "storyfleet.db")))

PROTECTED_JOBS = (329, 361, 362, 363, 364, 365, 366)
PROTECTED_DELIVERIES = (143, 144, 145, 148, 149, 150)
PROTECTED_GATEWAY = (6064, 6066, 6067, 6068, 6069)
EXPECTED_FK_COUNT = 910

SLOW_CYCLE_WARN_SEC = float(os.environ.get("READINESS_WORKER_SLOW_CYCLE_WARN_SEC", "120"))
STATUS_STALE_MULT = float(os.environ.get("READINESS_WORKER_STATUS_STALE_CYCLE_MULT", "2"))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _run(cmd: list[str], timeout: int = 10) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(_REPO))
        return (out.stdout or "").strip()
    except Exception:
        return ""


def _svc_state(name: str) -> dict[str, Any]:
    active = _run(["systemctl", "is-active", name]) or "unknown"
    enabled = _run(["systemctl", "is-enabled", name]) or "unknown"
    pid = _run(["systemctl", "show", name, "-p", "MainPID", "--value"])
    started = _run(["systemctl", "show", name, "-p", "ActiveEnterTimestamp", "--value"])
    return {"active": active, "enabled": enabled, "pid": pid, "active_enter_timestamp": started}


def _git_head() -> str:
    short = _run(["git", "-C", str(_REPO), "rev-parse", "--short", "HEAD"])
    return short or "unknown"


def _env_flags() -> dict[str, str]:
    keys = (
        "AUTOSTORY_PRODUCTION_CERTIFIED_NO_GO",
        "PROMO_GENERATION_MODE",
        "SCHEDULER_MUTATIONS_ENABLED",
        "P5C_SINGLE_SEND_ENABLED",
        "P5D_SINGLE_SEND_ENABLED",
        "READINESS_WORKER_ENABLED",
        "READINESS_WORKER_CYCLE_SEC",
        "READINESS_WORKER_BATCH_SIZE",
    )
    vals: dict[str, str] = {}
    env_path = _REPO / ".env"
    parsed: dict[str, str] = {}
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            parsed[k.strip()] = v.strip()
    for k in keys:
        vals[k] = os.environ.get(k) or parsed.get(k) or ""
    return vals


def _read_status_file() -> dict[str, Any]:
    try:
        if STATUS_FILE.is_file():
            return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _fk_fingerprint(conn) -> tuple[int, str]:
    rows = list(conn.execute("PRAGMA foreign_key_check"))
    digest = hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()[:16]
    return len(rows), digest


def _protected_fingerprint(conn) -> str:
    parts: list[Any] = []
    for jid in PROTECTED_JOBS:
        parts.append(conn.execute("SELECT id, status FROM scheduled_jobs WHERE id=?", (jid,)).fetchone())
    for did in PROTECTED_DELIVERIES:
        parts.append(conn.execute("SELECT id, status, tg_message_id FROM message_deliveries WHERE id=?", (did,)).fetchone())
    for gid in PROTECTED_GATEWAY:
        parts.append(conn.execute("SELECT id, status FROM telegram_gateway_jobs WHERE id=?", (gid,)).fetchone())
    return hashlib.sha256(json.dumps(parts, default=str).encode()).hexdigest()[:16]


def _queue_snapshot(conn) -> dict[str, Any]:
    pending = conn.execute(
        "SELECT COUNT(*) FROM scheduled_jobs WHERE status IN ('PENDING','RUNNING')"
    ).fetchone()[0]
    return {
        "pending_running": int(pending),
        "max_job_id": int(conn.execute("SELECT MAX(id) FROM scheduled_jobs").fetchone()[0] or 0),
        "max_delivery_id": int(conn.execute("SELECT MAX(id) FROM message_deliveries").fetchone()[0] or 0),
        "max_gateway_job_id": int(conn.execute("SELECT MAX(id) FROM telegram_gateway_jobs").fetchone()[0] or 0),
        "delivery_count": int(conn.execute("SELECT COUNT(*) FROM message_deliveries").fetchone()[0] or 0),
        "gateway_count": int(conn.execute("SELECT COUNT(*) FROM telegram_gateway_jobs").fetchone()[0] or 0),
        "job_count": int(conn.execute("SELECT COUNT(*) FROM scheduled_jobs").fetchone()[0] or 0),
    }


def _readiness_counts(conn) -> dict[str, int]:
    from src.clients import readiness_store

    fresh = stale = ready = not_auth = failed_auth = temp = 0
    now = _utc_now().replace(tzinfo=None)
    for row in conn.execute("SELECT account_id, status, failure_code, checked_at, expires_at FROM account_readiness_snapshots"):
        status = (row[1] or "").upper()
        if status == readiness_store.STAT_READY:
            ready += 1
            exp = row[4]
            if exp is not None and isinstance(exp, str):
                try:
                    exp_dt = datetime.fromisoformat(exp)
                except ValueError:
                    exp_dt = None
            else:
                exp_dt = row[4]
            if exp_dt is not None and isinstance(exp_dt, datetime) and exp_dt < now:
                stale += 1
            else:
                fresh += 1
        elif status == readiness_store.STAT_NOT_AUTH:
            not_auth += 1
            if (row[2] or "").lower() in ("unauthorized_session", "failed_auth"):
                failed_auth += 1
        elif status == readiness_store.STAT_TEMP:
            temp += 1
    return {
        "ready": ready,
        "fresh_ready": fresh,
        "stale_ready": stale,
        "not_authorized": not_auth,
        "failed_auth": failed_auth,
        "temporary_probe_failure": temp,
    }


def _account_row(conn, aid: int) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT account_id, status, checked_at, expires_at, failure_code, reason FROM account_readiness_snapshots WHERE account_id=?",
        (aid,),
    ).fetchone()
    if row is None:
        return None
    return {
        "account_id": row[0],
        "status": row[1],
        "checked_at": row[2],
        "expires_at": row[3],
        "failure_code": row[4],
        "reason": row[5],
    }


def collect_checkpoint(*, baseline: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    import sqlite3

    now = _utc_now()
    status = _read_status_file()
    env_flags = _env_flags()
    services = {
        "autostory-web": _svc_state("autostory-web"),
        "autostory-scheduler": _svc_state("autostory-scheduler"),
        "autostory-readiness-worker": _svc_state("autostory-readiness-worker"),
        "telegram-gateway": _svc_state("telegram-gateway"),
        "kathleen-account-listener": _svc_state("kathleen-account-listener"),
        "storyfleet-bot": _svc_state("storyfleet-bot"),
    }

    conn = sqlite3.connect(str(DB_PATH))
    try:
        quick = conn.execute("PRAGMA quick_check").fetchone()[0]
        fk_count, fk_fp = _fk_fingerprint(conn)
        queue = _queue_snapshot(conn)
        readiness = _readiness_counts(conn)
        protected_fp = _protected_fingerprint(conn)
        acc107 = _account_row(conn, 107)
        acc139 = _account_row(conn, 139)
        db_size = DB_PATH.stat().st_size if DB_PATH.is_file() else 0
        wal = DB_PATH.with_suffix(".db-wal")
        shm = DB_PATH.with_suffix(".db-shm")
    finally:
        conn.close()

    cycle_sec = float(status.get("cycle_duration_sec") or 0)
    cycle_sleep = float(status.get("next_cycle_sleep_sec") or env_flags.get("READINESS_WORKER_CYCLE_SEC") or 60)
    status_age_sec: Optional[float] = None
    updated = status.get("updated_at")
    if updated:
        try:
            u = datetime.fromisoformat(str(updated))
            status_age_sec = (now.replace(tzinfo=None) - u).total_seconds()
        except ValueError:
            pass

    payload: dict[str, Any] = {
        "checkpoint_utc": _iso(now),
        "git_head": _git_head(),
        "soak_id": (baseline or {}).get("soak_id"),
        "services": services,
        "env_flags": env_flags,
        "worker_status_file": status,
        "worker_status_age_sec": status_age_sec,
        "queue": queue,
        "readiness": readiness,
        "account_107": acc107,
        "account_139": acc139,
        "database": {
            "path": str(DB_PATH),
            "quick_check": quick,
            "fk_count": fk_count,
            "fk_fingerprint": fk_fp,
            "size_bytes": db_size,
            "wal_size_bytes": wal.stat().st_size if wal.is_file() else 0,
            "shm_size_bytes": shm.stat().st_size if shm.is_file() else 0,
            "readiness_row_count": readiness["ready"] + readiness["not_authorized"] + readiness["temporary_probe_failure"],
        },
        "protected_evidence_fingerprint": protected_fp,
        "cycle_metrics": {
            "last_cycle_duration_sec": cycle_sec,
            "slow_cycle_warn_sec": SLOW_CYCLE_WARN_SEC,
            "selection_duration_sec": status.get("selection_duration_sec"),
            "probe_duration_sec": status.get("probe_duration_sec"),
        },
    }

    blockers: list[str] = []
    warnings: list[str] = []

    if services["telegram-gateway"]["active"] == "active":
        blockers.append("telegram-gateway active")
    if services["kathleen-account-listener"]["active"] == "active":
        blockers.append("kathleen-account-listener active")
    if services["storyfleet-bot"]["active"] == "active":
        blockers.append("storyfleet-bot active")
    if env_flags.get("AUTOSTORY_PRODUCTION_CERTIFIED_NO_GO", "").lower() not in ("true", "1", "yes", "on"):
        blockers.append("NO_GO not true")
    if env_flags.get("SCHEDULER_MUTATIONS_ENABLED", "").lower() in ("true", "1", "yes", "on"):
        blockers.append("scheduler mutations enabled")
    if env_flags.get("PROMO_GENERATION_MODE", "").lower() not in ("", "disabled", "off", "false"):
        blockers.append("promo generation enabled")
    if env_flags.get("P5C_SINGLE_SEND_ENABLED", "").lower() in ("true", "1", "yes", "on"):
        blockers.append("P5C single-send enabled")
    if env_flags.get("P5D_SINGLE_SEND_ENABLED", "").lower() in ("true", "1", "yes", "on"):
        blockers.append("P5D single-send enabled")
    if not env_flags.get("P5D_SINGLE_SEND_ENABLED", "").strip():
        warnings.append("P5D_SINGLE_SEND_ENABLED unset (code defaults to false)")

    if services["autostory-readiness-worker"]["active"] != "active":
        blockers.append("readiness worker inactive")
    if quick != "ok":
        blockers.append(f"database quick_check={quick}")
    if fk_count != EXPECTED_FK_COUNT:
        warnings.append(f"FK count {fk_count} != expected legacy {EXPECTED_FK_COUNT}")

    if status_age_sec is not None and status_age_sec > cycle_sleep * STATUS_STALE_MULT:
        warnings.append(f"worker status file stale ({status_age_sec:.0f}s)")
    if cycle_sec > SLOW_CYCLE_WARN_SEC:
        warnings.append(f"slow cycle duration {cycle_sec}s (threshold {SLOW_CYCLE_WARN_SEC}s)")

    if baseline:
        bq = baseline.get("queue") or {}
        for key in ("pending_running", "max_job_id", "max_delivery_id", "max_gateway_job_id", "delivery_count"):
            if queue.get(key) != bq.get(key):
                blockers.append(f"queue invariant changed: {key} {bq.get(key)} -> {queue.get(key)}")
        if protected_fp != baseline.get("protected_evidence_fingerprint"):
            blockers.append("protected evidence fingerprint changed")

    acc139 = payload.get("account_139") or {}
    if acc139.get("status") not in (None, "NOT_AUTHORIZED"):
        warnings.append(f"account 139 status unexpected: {acc139.get('status')}")

    payload["evaluation"] = {
        "blockers": blockers,
        "warnings": warnings,
        "exit_code": 2 if blockers else (1 if warnings else 0),
    }
    return payload


def _find_baseline() -> Optional[dict[str, Any]]:
    files = sorted((_REPO / "data" / "audit").glob(BASELINE_GLOB))
    if not files:
        return None
    return json.loads(files[-1].read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="P6.2A readiness worker soak checkpoint")
    parser.add_argument("--write-baseline", action="store_true", help="Create immutable soak baseline")
    parser.add_argument("--soak-id", default="", help="Soak identifier for baseline")
    parser.add_argument("--baseline-path", default="", help="Explicit baseline JSON path")
    args = parser.parse_args()

    now = _utc_now()
    if args.write_baseline:
        import sqlite3

        conn = sqlite3.connect(str(DB_PATH))
        try:
            fk_count, fk_fp = _fk_fingerprint(conn)
            queue = _queue_snapshot(conn)
            protected_fp = _protected_fingerprint(conn)
            acc107 = _account_row(conn, 107)
            acc139 = _account_row(conn, 139)
        finally:
            conn.close()
        soak_id = args.soak_id or f"p6_2_soak_{now.strftime('%Y%m%dT%H%M%SZ')}"
        baseline = {
            "soak_id": soak_id,
            "soak_start_utc": _iso(now),
            "git_head": _git_head(),
            "services": {
                k: _svc_state(k)
                for k in (
                    "autostory-web",
                    "autostory-scheduler",
                    "autostory-readiness-worker",
                    "telegram-gateway",
                    "kathleen-account-listener",
                    "storyfleet-bot",
                )
            },
            "env_flags": _env_flags(),
            "queue": queue,
            "protected_evidence_fingerprint": protected_fp,
            "database": {"fk_count": fk_count, "fk_fingerprint": fk_fp},
            "account_107": acc107,
            "account_139": acc139,
        }
        out = _REPO / "data" / "audit" / f"p6_2_soak_baseline_{now.strftime('%Y%m%dT%H%M%SZ')}.json"
        out.write_text(json.dumps(baseline, indent=2, sort_keys=True), encoding="utf-8")
        print(json.dumps({"baseline_path": str(out), "soak_id": soak_id}, indent=2))
        return 0

    baseline = None
    if args.baseline_path:
        baseline = json.loads(Path(args.baseline_path).read_text(encoding="utf-8"))
    else:
        baseline = _find_baseline()

    payload = collect_checkpoint(baseline=baseline)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    out = AUDIT_DIR / f"checkpoint_{now.strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    latest = AUDIT_DIR / "checkpoint_latest.json"
    latest.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    md = _REPO / "data" / "audit" / f"p6_2_soak_checkpoint_{now.strftime('%Y%m%dT%H%M%SZ')}.md"
    ev = payload["evaluation"]
    md.write_text(
        "\n".join(
            [
                f"# P6.2A Soak Checkpoint {_iso(now)}",
                f"- exit_code: {ev['exit_code']}",
                f"- blockers: {ev['blockers'] or 'none'}",
                f"- warnings: {ev['warnings'] or 'none'}",
                f"- git: {payload['git_head']}",
                f"- worker cycle sec: {payload['cycle_metrics']['last_cycle_duration_sec']}",
            ]
        ),
        encoding="utf-8",
    )
    print(json.dumps({"checkpoint": str(out), "evaluation": ev}, indent=2))
    return int(ev["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
