"""Automatic / operator fleet matrix refresh (Wave 8).

Runs the already-certified read-only fleet certification path, then builds and
atomically writes the canonical Accounts matrix.

Safety:
- Fail-closed mutation flags for THIS process only (never edits production .env).
- Non-blocking flock: overlapping runs skip without a second fleet Telethon pass.
- Failed builds leave the previous latest.json untouched.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Fail-closed BEFORE importing Storyfleet modules that might read settings.
# These override any inherited EnvironmentFile values for THIS process only.
# ---------------------------------------------------------------------------
_FAIL_CLOSED_ENV = {
    "STORY_EXECUTION_ENABLED": "false",
    "CONTROLLED_STORY_EXECUTION_ENABLED": "false",
    "SCHEDULER_STORY_EXECUTION_ENABLED": "false",
    "SCHEDULER_MUTATIONS_ENABLED": "false",
    "CAMPAIGN_EXECUTION_ENABLED": "false",
    "DISCOVERY_EXECUTION_ENABLED": "false",
    "BROADCAST_EXECUTION_ENABLED": "false",
    "TELEGRAM_STORIES_LIVE_ENABLED": "false",
    "STORY_MUTATIONS_ENABLED": "false",
    # Do NOT disable production Messages globally — only this process.
    "MESSAGES_EXECUTION_ENABLED": "false",
    "MESSAGES_AI_DRAFT_ENABLED": "false",
    "AI_AGENT_AUTO_LOOP_ENABLED": "false",
}


def _apply_fail_closed_env() -> None:
    for key, value in _FAIL_CLOSED_ENV.items():
        os.environ[key] = value
    os.environ.pop("CONTROLLED_STORY_ACCOUNT_ID", None)


_apply_fail_closed_env()

DEFAULT_LOCK_PATH = Path("/var/lock/autostory-fleet-matrix-refresh.lock")
DEFAULT_LATEST = Path("/opt/autostory/data/fleet-readiness/latest.json")
DEFAULT_BACKUP_DIR = Path("/opt/autostory/data/fleet-readiness/backups")
DEFAULT_STATUS_PATH = Path("/opt/autostory/data/fleet-readiness/refresh_status.json")
DEFAULT_AUTO_AUDIT_ROOT = Path("/opt/autostory/data/audit/fleet-matrix-auto")
DEFAULT_KEEP_AUTO_EVIDENCE = 8
DEFAULT_KEEP_BACKUPS = 12
REGENERATION_METHOD = "build_canonical_matrix_from_fleet_certification_read_only"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_status(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _prune_dirs(parent: Path, *, keep: int, prefix: str = "") -> int:
    """Delete oldest matching subdirectories beyond ``keep``. Returns deleted count."""
    if keep < 1 or not parent.is_dir():
        return 0
    dirs = [
        p
        for p in parent.iterdir()
        if p.is_dir() and (not prefix or p.name.startswith(prefix))
    ]
    dirs.sort(key=lambda p: p.name)
    deleted = 0
    for old in dirs[:-keep] if len(dirs) > keep else []:
        shutil.rmtree(old, ignore_errors=True)
        deleted += 1
    return deleted


def _prune_files(parent: Path, *, keep: int, glob_pat: str) -> int:
    if keep < 1 or not parent.is_dir():
        return 0
    files = sorted(parent.glob(glob_pat), key=lambda p: p.name)
    deleted = 0
    for old in files[:-keep] if len(files) > keep else []:
        try:
            old.unlink()
            deleted += 1
        except OSError:
            pass
    return deleted


def _operator_counts(matrix: dict[str, Any]) -> dict[str, int]:
    hist = (matrix.get("totals") or {}).get("classification_histogram") or {}
    return {
        "total": int((matrix.get("totals") or {}).get("total_configured_accounts") or len(matrix.get("accounts") or [])),
        "certified": int(hist.get("CERTIFIED_PUBLISH", 0)),
        "auth_failed": int(hist.get("AUTH_FAILED", 0)),
        "disabled": int(hist.get("ACCOUNT_DISABLED", 0)),
        "excluded": int(hist.get("INTENTIONALLY_EXCLUDED", 0)),
        "check_required": int(hist.get("UNKNOWN", 0)),
    }


def refresh_fleet_matrix(
    *,
    output_root: Path = DEFAULT_AUTO_AUDIT_ROOT,
    latest_path: Path = DEFAULT_LATEST,
    backup_dir: Path = DEFAULT_BACKUP_DIR,
    status_path: Path = DEFAULT_STATUS_PATH,
    lock_path: Path = DEFAULT_LOCK_PATH,
    timeout_seconds: float = 15.0,
    keep_auto_evidence: int = DEFAULT_KEEP_AUTO_EVIDENCE,
    keep_backups: int = DEFAULT_KEEP_BACKUPS,
    skip_if_locked: bool = True,
) -> dict[str, Any]:
    """Run certified read-only refresh. Returns a status dict."""
    _apply_fail_closed_env()
    started = utc_now_iso()
    t0 = time.monotonic()
    status: dict[str, Any] = {
        "ok": False,
        "started_at": started,
        "completed_at": None,
        "duration_sec": None,
        "skipped": False,
        "error": None,
        "matrix_generated_at": None,
        "source_audit_run_id": None,
        "evidence_dir": None,
        "counts": None,
        "fail_closed_env": dict(_FAIL_CLOSED_ENV),
        "regeneration_method": REGENERATION_METHOD,
    }

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fh = open(lock_path, "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            status["skipped"] = True
            status["error"] = "LOCK_BUSY"
            status["completed_at"] = utc_now_iso()
            status["duration_sec"] = round(time.monotonic() - t0, 3)
            _write_status(status_path, status)
            return status

        # Import after fail-closed env is set.
        from src.stories.fleet_certification import audit_fleet, write_artifacts
        from src.stories.fleet_readiness_matrix import (
            build_canonical_matrix,
            write_latest_matrix,
        )
        import asyncio

        # Backup current matrix (never delete on failure).
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        if latest_path.is_file():
            backup_path = backup_dir / f"latest.json.pre-auto-refresh-{stamp}"
            shutil.copy2(latest_path, backup_path)
            status["backup_path"] = str(backup_path)

        output_root.mkdir(parents=True, exist_ok=True)
        report = asyncio.run(audit_fleet(timeout_seconds=timeout_seconds))
        evidence_dir = write_artifacts(report, output_root)
        status["evidence_dir"] = str(evidence_dir)
        status["source_audit_run_id"] = report.get("audit_run_id")

        audit_path = evidence_dir / "fleet-readiness.json"
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        matrix = build_canonical_matrix(audit)
        matrix["regeneration_method"] = REGENERATION_METHOD
        matrix["source_evidence_dir"] = str(evidence_dir)
        write_latest_matrix(matrix, latest_path)

        counts = _operator_counts(matrix)
        status["ok"] = True
        status["matrix_generated_at"] = matrix.get("generated_at")
        status["counts"] = counts
        status["production_mutations"] = report.get("production_mutations")

        # Retention: auto evidence + matrix backups only (never touch manual audits).
        status["pruned_evidence"] = _prune_dirs(
            output_root, keep=keep_auto_evidence, prefix="fleet-certification-"
        )
        status["pruned_backups"] = _prune_files(
            backup_dir, keep=keep_backups, glob_pat="latest.json.pre-auto-refresh-*"
        )
    except Exception as exc:
        status["ok"] = False
        status["error"] = f"{type(exc).__name__}: {exc}"
        status["traceback"] = traceback.format_exc(limit=20)
    finally:
        status["completed_at"] = utc_now_iso()
        status["duration_sec"] = round(time.monotonic() - t0, 3)
        try:
            _write_status(status_path, status)
        except Exception:
            pass
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        lock_fh.close()

    return status


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Wave 8 automatic fleet matrix refresh (read-only)")
    p.add_argument("--timeout-seconds", type=float, default=15.0)
    p.add_argument(
        "--output-root",
        default=str(DEFAULT_AUTO_AUDIT_ROOT),
        help="Evidence parent for automatic runs (retained/pruned separately from manual audits)",
    )
    p.add_argument("--latest-path", default=str(DEFAULT_LATEST))
    p.add_argument("--status-path", default=str(DEFAULT_STATUS_PATH))
    p.add_argument("--lock-path", default=str(DEFAULT_LOCK_PATH))
    p.add_argument("--keep-auto-evidence", type=int, default=DEFAULT_KEEP_AUTO_EVIDENCE)
    p.add_argument("--keep-backups", type=int, default=DEFAULT_KEEP_BACKUPS)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.timeout_seconds <= 0 or args.timeout_seconds > 60:
        print("ERROR: --timeout-seconds must be between 0 and 60", file=sys.stderr)
        return 2
    result = refresh_fleet_matrix(
        output_root=Path(args.output_root),
        latest_path=Path(args.latest_path),
        status_path=Path(args.status_path),
        lock_path=Path(args.lock_path),
        timeout_seconds=float(args.timeout_seconds),
        keep_auto_evidence=int(args.keep_auto_evidence),
        keep_backups=int(args.keep_backups),
    )
    # Operator-safe summary (no secrets).
    summary = {
        "ok": result.get("ok"),
        "skipped": result.get("skipped"),
        "error": result.get("error"),
        "duration_sec": result.get("duration_sec"),
        "matrix_generated_at": result.get("matrix_generated_at"),
        "source_audit_run_id": result.get("source_audit_run_id"),
        "counts": result.get("counts"),
        "evidence_dir": result.get("evidence_dir"),
    }
    print(json.dumps(summary, sort_keys=True))
    if result.get("skipped"):
        return 0  # overlap skip is non-fatal for timer
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
