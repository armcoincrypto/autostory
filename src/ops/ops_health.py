"""Wave L — consolidated Storyfleet ops health (read-only, cheap).

No Telegram fleet probes. No OpenAI probe calls. Observes existing evidence only.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from src.ops.ai_draft_metrics import summarize_ai_draft_metrics

STATE_HEALTHY = "healthy"
STATE_WARNING = "warning"
STATE_CRITICAL = "critical"
STATE_INFO = "info"  # policy-disabled / expected

RUNTIME_DIR = Path(os.environ.get("STORYFLEET_RUNTIME_DIR") or "/opt/autostory/data/runtime")
LATEST_PATH = RUNTIME_DIR / "ops_health_latest.json"
ALERT_STATE_PATH = RUNTIME_DIR / "ops_health_alert_state.json"
FLEET_LATEST = Path("/opt/autostory/data/fleet-readiness/latest.json")
FLEET_REFRESH = Path("/opt/autostory/data/fleet-readiness/refresh_status.json")
BACKUP_STATUS = Path(
    os.environ.get("STORYFLEET_OFFHOST_STATUS")
    or str(RUNTIME_DIR / "offhost_backup_status.json")
)
DB_PATH = Path(os.environ.get("DATABASE_PATH") or "/opt/autostory/data/storyfleet.db")

MATRIX_TTL_H = 24.0
MATRIX_WARN_H = 18.0
BACKUP_MAX_AGE_H = float(os.environ.get("STORYFLEET_BACKUP_MAX_AGE_HOURS") or "36")
DISK_WARN = int(os.environ.get("STORYFLEET_DISK_WARN_PCT") or "80")
DISK_CRIT = int(os.environ.get("STORYFLEET_DISK_CRIT_PCT") or "90")
WAL_WARN_BYTES = int(os.environ.get("STORYFLEET_WAL_WARN_BYTES") or str(64 * 1024 * 1024))
SENDING_STALE_MIN = int(os.environ.get("STORYFLEET_SENDING_STALE_MIN") or "15")
FAILED_SPIKE = int(os.environ.get("STORYFLEET_MSG_FAILED_SPIKE") or "5")
CERTIFIED_DROP_WARN = int(os.environ.get("STORYFLEET_CERTIFIED_DROP_WARN") or "5")
QUICK_CHECK_MAX_AGE_H = float(os.environ.get("STORYFLEET_DB_QUICK_CHECK_HOURS") or "24")
HEALTH_HISTORY_RETENTION = "ops_health_latest.json overwrite + alert_state; ai_draft_metrics.jsonl capped"


REQUIRED_UNITS = (
    "autostory-web.service",
    "autostory-scheduler.service",
    "autostory-readiness-worker.service",
)
REQUIRED_TIMERS = (
    "autostory-fleet-matrix-refresh.timer",
    "storyfleet-offhost-backup.timer",
    "storyfleet-backup-age-check.timer",
    "storyfleet-retention-maintenance.timer",
)


@dataclass
class Check:
    name: str
    state: str
    summary: str
    detail: Optional[dict[str, Any]] = None

    def as_dict(self) -> dict[str, Any]:
        out = {
            "name": self.name,
            "state": self.state,
            "summary": self.summary,
            "timestamp": _utcnow_iso(),
        }
        if self.detail:
            out["detail"] = self.detail
        return out


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat().replace("+00:00", "Z")


def _parse_iso(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _env_flag(name: str, default: str = "false") -> bool:
    raw = (os.environ.get(name) or default).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _systemctl_is_active(unit: str) -> str:
    try:
        proc = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return (proc.stdout or "").strip() or "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def _check_units() -> list[Check]:
    checks: list[Check] = []
    for unit in REQUIRED_UNITS:
        active = _systemctl_is_active(unit)
        short = unit.replace(".service", "")
        if active == "active":
            checks.append(Check(short, STATE_HEALTHY, f"{short} is running"))
        else:
            checks.append(
                Check(
                    short,
                    STATE_CRITICAL,
                    f"{short} is not active ({active})",
                    {"unit": unit, "active": active},
                )
            )
    for unit in REQUIRED_TIMERS:
        active = _systemctl_is_active(unit)
        short = unit.replace(".timer", "")
        if active == "active":
            checks.append(Check(f"timer:{short}", STATE_HEALTHY, f"{short} timer active"))
        else:
            checks.append(
                Check(
                    f"timer:{short}",
                    STATE_WARNING,
                    f"{short} timer not active ({active})",
                    {"unit": unit, "active": active},
                )
            )
    return checks


def _check_fleet_matrix(*, previous_counts: Optional[dict[str, int]] = None) -> list[Check]:
    checks: list[Check] = []
    refresh: dict[str, Any] = {}
    if FLEET_REFRESH.is_file():
        try:
            refresh = json.loads(FLEET_REFRESH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            refresh = {}
    err = refresh.get("error")
    if err:
        checks.append(
            Check(
                "fleet_matrix_refresh",
                STATE_CRITICAL,
                "Fleet matrix last refresh failed",
                {"error": str(err)[:200]},
            )
        )
    else:
        completed = refresh.get("completed_at")
        checks.append(
            Check(
                "fleet_matrix_refresh",
                STATE_HEALTHY,
                f"Fleet matrix refresh ok ({completed or 'unknown time'})",
            )
        )

    if not FLEET_LATEST.is_file():
        checks.append(Check("fleet_matrix", STATE_CRITICAL, "Fleet matrix file missing"))
        return checks

    try:
        matrix = json.loads(FLEET_LATEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        checks.append(Check("fleet_matrix", STATE_CRITICAL, "Fleet matrix file malformed"))
        return checks

    generated = _parse_iso(matrix.get("generated_at"))
    if generated is None:
        checks.append(Check("fleet_matrix", STATE_CRITICAL, "Fleet matrix missing timestamp"))
        return checks

    age_h = (_utcnow() - generated).total_seconds() / 3600.0
    if age_h >= MATRIX_TTL_H:
        state, summary = STATE_CRITICAL, f"Fleet matrix stale ({age_h:.1f}h > {MATRIX_TTL_H:.0f}h TTL)"
    elif age_h >= MATRIX_WARN_H:
        state, summary = STATE_WARNING, f"Fleet matrix aging ({age_h:.1f}h; refresh before {MATRIX_TTL_H:.0f}h)"
    else:
        state, summary = STATE_HEALTHY, f"Fleet matrix fresh ({age_h:.1f}h old)"
    totals = matrix.get("totals") or refresh.get("counts") or {}
    checks.append(
        Check(
            "fleet_matrix",
            state,
            summary,
            {
                "age_hours": round(age_h, 2),
                "certified": totals.get("certified"),
                "auth_failed": totals.get("auth_failed"),
                "check_required": totals.get("check_required"),
            },
        )
    )

    # Degradation vs previous snapshot (material change only)
    certified = int(totals.get("certified") or 0)
    auth_failed = int(totals.get("auth_failed") or 0)
    check_required = int(totals.get("check_required") or 0)
    if check_required > 0:
        checks.append(
            Check(
                "fleet_degraded",
                STATE_WARNING,
                f"{check_required} account(s) need check",
                {"check_required": check_required},
            )
        )
    elif previous_counts:
        prev_c = int(previous_counts.get("certified") or certified)
        drop = prev_c - certified
        if drop >= CERTIFIED_DROP_WARN:
            checks.append(
                Check(
                    "fleet_degraded",
                    STATE_WARNING,
                    f"Certified accounts dropped by {drop}",
                    {"previous_certified": prev_c, "certified": certified},
                )
            )
        elif auth_failed > int(previous_counts.get("auth_failed") or 0):
            checks.append(
                Check(
                    "fleet_degraded",
                    STATE_WARNING,
                    f"AUTH_FAILED increased to {auth_failed}",
                    {"auth_failed": auth_failed},
                )
            )
        else:
            checks.append(Check("fleet_degraded", STATE_HEALTHY, "No material fleet degradation"))
    else:
        checks.append(Check("fleet_degraded", STATE_HEALTHY, "Fleet counts stable / baseline recorded"))

    return checks


def _check_backup() -> Check:
    if not BACKUP_STATUS.is_file():
        return Check("backup", STATE_CRITICAL, "Backup status file missing")
    try:
        data = json.loads(BACKUP_STATUS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Check("backup", STATE_CRITICAL, "Backup status file malformed")

    state_raw = str(data.get("state") or "").lower()
    finished = _parse_iso(data.get("finished_at_utc") or data.get("finished_at"))
    if state_raw in {"failed", "error"}:
        return Check(
            "backup",
            STATE_CRITICAL,
            "Last off-host backup failed",
            {"state": state_raw, "message": str(data.get("message") or "")[:160]},
        )
    if finished is None:
        return Check("backup", STATE_WARNING, "Backup finished time unknown")
    age_h = (_utcnow() - finished).total_seconds() / 3600.0
    if age_h > BACKUP_MAX_AGE_H:
        return Check(
            "backup",
            STATE_CRITICAL,
            f"Backup stale ({age_h:.1f}h > {BACKUP_MAX_AGE_H:.0f}h)",
            {"age_hours": round(age_h, 2)},
        )
    return Check(
        "backup",
        STATE_HEALTHY,
        f"Backup ok ({age_h:.1f}h ago)",
        {"age_hours": round(age_h, 2), "off_host": data.get("off_host")},
    )


def _check_disk() -> Check:
    try:
        usage = shutil.disk_usage("/")
        pct = int(round(100.0 * (usage.used / max(usage.total, 1))))
    except OSError:
        return Check("disk", STATE_WARNING, "Disk usage unavailable")
    if pct >= DISK_CRIT:
        return Check("disk", STATE_CRITICAL, f"Disk critical ({pct}% used)", {"used_pct": pct})
    if pct >= DISK_WARN:
        return Check("disk", STATE_WARNING, f"Disk warning ({pct}% used)", {"used_pct": pct})
    return Check("disk", STATE_HEALTHY, f"Disk ok ({pct}% used)", {"used_pct": pct})


def _check_database(*, run_quick_check: bool = False) -> list[Check]:
    checks: list[Check] = []
    if not DB_PATH.is_file():
        return [Check("database", STATE_CRITICAL, "Database file missing")]
    try:
        size = DB_PATH.stat().st_size
        wal = Path(str(DB_PATH) + "-wal")
        wal_size = wal.stat().st_size if wal.is_file() else 0
    except OSError:
        return [Check("database", STATE_CRITICAL, "Database file unreadable")]

    if wal_size >= WAL_WARN_BYTES:
        checks.append(
            Check(
                "database_wal",
                STATE_WARNING,
                f"SQLite WAL large ({wal_size // (1024 * 1024)} MiB)",
                {"wal_bytes": wal_size},
            )
        )
    else:
        checks.append(
            Check("database_wal", STATE_HEALTHY, f"SQLite WAL ok ({wal_size // (1024 * 1024)} MiB)")
        )

    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
        try:
            con.execute("SELECT 1").fetchone()
            if run_quick_check:
                row = con.execute("PRAGMA quick_check").fetchone()
                ok = row and str(row[0]).lower() == "ok"
                if ok:
                    checks.append(Check("database_quick_check", STATE_HEALTHY, "PRAGMA quick_check ok"))
                else:
                    checks.append(
                        Check(
                            "database_quick_check",
                            STATE_CRITICAL,
                            "PRAGMA quick_check reported issues",
                            {"result": str(row[0])[:120] if row else None},
                        )
                    )
            else:
                checks.append(
                    Check(
                        "database",
                        STATE_HEALTHY,
                        f"Database readable ({size // (1024 * 1024)} MiB)",
                    )
                )
        finally:
            con.close()
    except sqlite3.Error as exc:
        checks.append(
            Check("database", STATE_CRITICAL, "Database not readable", {"error": type(exc).__name__})
        )
    return checks


def _check_messages() -> list[Check]:
    checks: list[Check] = []
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return [Check("messages", STATE_WARNING, "Messages status unavailable")]
    try:
        uncertain = con.execute(
            "SELECT count(*) FROM owner_dm_intents WHERE upper(status)='UNCERTAIN'"
        ).fetchone()[0]
        since = (_utcnow() - timedelta(hours=24)).replace(tzinfo=None).isoformat(sep=" ")
        failed_24h = con.execute(
            "SELECT count(*) FROM owner_dm_intents WHERE upper(status)='FAILED' AND created_at >= ?",
            (since,),
        ).fetchone()[0]
        stale_cutoff = (_utcnow() - timedelta(minutes=SENDING_STALE_MIN)).replace(tzinfo=None).isoformat(
            sep=" "
        )
        stale_sending = con.execute(
            "SELECT count(*) FROM owner_dm_intents WHERE upper(status)='SENDING' "
            "AND coalesce(updated_at, created_at) < ?",
            (stale_cutoff,),
        ).fetchone()[0]
    except sqlite3.Error:
        return [Check("messages", STATE_WARNING, "Messages tables unavailable")]
    finally:
        con.close()

    if uncertain > 0:
        checks.append(
            Check(
                "messages_uncertain",
                STATE_CRITICAL,
                f"{uncertain} UNCERTAIN message intent(s) need attention",
                {"uncertain": uncertain},
            )
        )
    else:
        checks.append(Check("messages_uncertain", STATE_HEALTHY, "No UNCERTAIN message intents"))

    if failed_24h >= FAILED_SPIKE:
        checks.append(
            Check(
                "messages_failed",
                STATE_WARNING,
                f"{failed_24h} FAILED message intents in 24h",
                {"failed_24h": failed_24h},
            )
        )
    else:
        checks.append(
            Check("messages_failed", STATE_HEALTHY, f"FAILED intents in 24h: {failed_24h}")
        )

    if stale_sending > 0:
        checks.append(
            Check(
                "messages_stale_sending",
                STATE_WARNING,
                f"{stale_sending} SENDING intent(s) older than {SENDING_STALE_MIN}m",
            )
        )
    else:
        checks.append(Check("messages_stale_sending", STATE_HEALTHY, "No stale SENDING intents"))

    return checks


def _check_scheduler() -> list[Check]:
    checks: list[Check] = []
    # Process covered by unit check; inspect job anomalies cheaply.
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return [Check("scheduler_jobs", STATE_WARNING, "Scheduler job table unavailable")]
    try:
        now = _utcnow().replace(tzinfo=None).isoformat(sep=" ")
        overdue = con.execute(
            "SELECT count(*) FROM scheduled_jobs WHERE upper(status)='PENDING' "
            "AND run_at IS NOT NULL AND run_at < ?",
            (now,),
        ).fetchone()[0]
        # Stuck RUNNING: updated older than 2h
        stuck_cut = (_utcnow() - timedelta(hours=2)).replace(tzinfo=None).isoformat(sep=" ")
        try:
            stuck = con.execute(
                "SELECT count(*) FROM scheduled_jobs WHERE upper(status)='RUNNING' "
                "AND coalesce(updated_at, created_at) < ?",
                (stuck_cut,),
            ).fetchone()[0]
        except sqlite3.Error:
            stuck = 0
    except sqlite3.Error:
        return [Check("scheduler_jobs", STATE_HEALTHY, "Scheduler job query skipped")]
    finally:
        con.close()

    # Mutations disabled + zero overdue is fine; overdue PENDING is warning only if
    # SCHEDULER_MUTATIONS or generation could create work — still surface overdue.
    if stuck > 0:
        checks.append(
            Check("scheduler_stuck", STATE_WARNING, f"{stuck} stuck RUNNING scheduler job(s)")
        )
    else:
        checks.append(Check("scheduler_stuck", STATE_HEALTHY, "No stuck RUNNING scheduler jobs"))

    if overdue > 0 and _env_flag("SCHEDULER_MUTATIONS_ENABLED", "false"):
        # Worker active but owner mutations off — overdue may be historical; warn softly.
        checks.append(
            Check(
                "scheduler_overdue",
                STATE_INFO,
                f"{overdue} PENDING job(s) past due (mutations disabled — review only)",
                {"overdue": overdue},
            )
        )
    elif overdue > 0:
        checks.append(
            Check("scheduler_overdue", STATE_WARNING, f"{overdue} PENDING job(s) past due")
        )
    else:
        checks.append(Check("scheduler_overdue", STATE_HEALTHY, "No overdue PENDING jobs"))

    return checks


def _check_scheduled_dm() -> Check:
    if not _env_flag("SCHEDULED_DM_ENABLED", "false"):
        return Check(
            "scheduled_dm",
            STATE_INFO,
            "Scheduled DM intentionally disabled",
            {"state": "OK_POLICY_DISABLED"},
        )
    # Future: detect overdue PENDING / stuck RUNNING / UNCERTAIN for DM jobs.
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
        # Table may not exist yet — fail soft.
        tables = {
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "scheduled_dm_jobs" not in tables and "owner_dm_schedules" not in tables:
            con.close()
            return Check(
                "scheduled_dm",
                STATE_WARNING,
                "Scheduled DM enabled but job table not found",
            )
        con.close()
    except sqlite3.Error:
        return Check("scheduled_dm", STATE_WARNING, "Scheduled DM status unavailable")
    return Check("scheduled_dm", STATE_HEALTHY, "Scheduled DM enabled; no stuck-job signal")


def _check_broadcast() -> Check:
    if not _env_flag("BROADCAST_EXECUTION_ENABLED", "false"):
        return Check(
            "broadcast",
            STATE_INFO,
            "Broadcast intentionally disabled",
            {"state": "OK_POLICY_DISABLED"},
        )
    return Check(
        "broadcast",
        STATE_WARNING,
        "Broadcast enabled — verify certified executor before live use",
        {"state": "ENABLED_UNCERTIFIED_RISK"},
    )


def _check_ai_draft() -> Check:
    if not _env_flag("MESSAGES_AI_DRAFT_ENABLED", "false"):
        return Check(
            "ai_draft",
            STATE_INFO,
            "AI Draft intentionally disabled",
            {"state": "OK_POLICY_DISABLED"},
        )
    summary = summarize_ai_draft_metrics()
    if summary.get("failure_spike"):
        return Check(
            "ai_draft",
            STATE_WARNING,
            "AI Draft issue — repeated provider failures",
            {
                "consecutive_failures": summary.get("consecutive_failures"),
                "failures_in_window": summary.get("failures_in_window"),
                "failed_today": summary.get("failed_today"),
                "success_today": summary.get("success_today"),
                "prompts_persisted": False,
            },
        )
    return Check(
        "ai_draft",
        STATE_HEALTHY,
        f"AI Draft ok today ({summary.get('success_today', 0)} ok / {summary.get('failed_today', 0)} failed)",
        {
            "requests_today": summary.get("requests_today"),
            "input_tokens_today": summary.get("input_tokens_today"),
            "output_tokens_today": summary.get("output_tokens_today"),
            "avg_latency_ms_today": summary.get("avg_latency_ms_today"),
            "prompts_persisted": False,
        },
    )


def _should_run_quick_check(state: dict[str, Any]) -> bool:
    last = _parse_iso(state.get("last_quick_check_at"))
    if last is None:
        return True
    return (_utcnow() - last).total_seconds() >= QUICK_CHECK_MAX_AGE_H * 3600


def _overall(checks: list[Check]) -> str:
    ranks = {STATE_CRITICAL: 3, STATE_WARNING: 2, STATE_INFO: 0, STATE_HEALTHY: 0}
    score = max((ranks.get(c.state, 0) for c in checks), default=0)
    if score >= 3:
        return STATE_CRITICAL
    if score >= 2:
        return STATE_WARNING
    return STATE_HEALTHY


def _owner_copy(status: str, checks: list[Check]) -> str:
    if status == STATE_HEALTHY:
        return "System healthy"
    material = [c for c in checks if c.state in {STATE_CRITICAL, STATE_WARNING}]
    if not material:
        return "System healthy"
    top = material[0]
    if status == STATE_CRITICAL:
        return f"System needs attention — {top.summary}"
    return f"System needs attention — {top.summary}"


def build_ops_health_report(
    *,
    previous_state: Optional[dict[str, Any]] = None,
    run_quick_check: Optional[bool] = None,
) -> dict[str, Any]:
    previous_state = previous_state or {}
    prev_counts = (previous_state.get("fleet_counts") or {}) if previous_state else {}
    do_qc = run_quick_check if run_quick_check is not None else _should_run_quick_check(previous_state)

    checks: list[Check] = []
    checks.extend(_check_units())
    checks.extend(_check_fleet_matrix(previous_counts=prev_counts or None))
    checks.append(_check_backup())
    checks.append(_check_disk())
    checks.extend(_check_database(run_quick_check=do_qc))
    checks.extend(_check_messages())
    checks.extend(_check_scheduler())
    checks.append(_check_scheduled_dm())
    checks.append(_check_broadcast())
    checks.append(_check_ai_draft())

    status = _overall(checks)
    # Capture current fleet counts for next comparison
    fleet_counts: dict[str, int] = {}
    for c in checks:
        if c.name == "fleet_matrix" and c.detail:
            for k in ("certified", "auth_failed", "check_required"):
                if c.detail.get(k) is not None:
                    try:
                        fleet_counts[k] = int(c.detail[k])
                    except (TypeError, ValueError):
                        pass

    report = {
        "ok": status != STATE_CRITICAL,
        "status": status,
        "owner_copy": _owner_copy(status, checks),
        "generated_at": _utcnow_iso(),
        "checks": [c.as_dict() for c in checks],
        "contracts": {
            "live_telegram_probes": 0,
            "openai_probes": 0,
            "monitor_can_send_customer_dm": False,
            "private_body_logged": False,
            "secrets_logged": False,
        },
        "retention": HEALTH_HISTORY_RETENTION,
        "fleet_counts": fleet_counts,
        "last_quick_check_at": _utcnow_iso() if do_qc else previous_state.get("last_quick_check_at"),
    }
    return report


def load_previous_state(path: Path | None = None) -> dict[str, Any]:
    target = path or LATEST_PATH
    if not target.is_file():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_ops_health_report(report: dict[str, Any], path: Path | None = None) -> Path:
    target = path or LATEST_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    tmp.replace(target)
    try:
        os.chmod(target, 0o640)
    except OSError:
        pass
    return target


def compute_alert_events(
    report: dict[str, Any],
    *,
    previous_alert_state: Optional[dict[str, Any]] = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Dedup material alerts. Returns (events_to_emit, new_alert_state)."""
    previous_alert_state = previous_alert_state or {}
    open_issues = dict(previous_alert_state.get("open") or {})
    events: list[dict[str, Any]] = []
    current_keys: set[str] = set()

    for check in report.get("checks") or []:
        state = check.get("state")
        if state not in {STATE_CRITICAL, STATE_WARNING}:
            continue
        # INFO policy states never alert
        key = f"{check.get('name')}:{state}"
        current_keys.add(key)
        if key not in open_issues:
            events.append(
                {
                    "type": "new",
                    "name": check.get("name"),
                    "state": state,
                    "summary": check.get("summary"),
                }
            )
            open_issues[key] = {
                "since": report.get("generated_at"),
                "summary": check.get("summary"),
            }

    recovered = []
    for key in list(open_issues.keys()):
        if key not in current_keys:
            recovered.append(key)
            events.append(
                {
                    "type": "recovered",
                    "name": key.split(":", 1)[0],
                    "state": STATE_HEALTHY,
                    "summary": f"Recovered: {open_issues[key].get('summary')}",
                }
            )
            open_issues.pop(key, None)

    new_state = {"updated_at": report.get("generated_at"), "open": open_issues}
    return events, new_state


def load_alert_state(path: Path | None = None) -> dict[str, Any]:
    target = path or ALERT_STATE_PATH
    if not target.is_file():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_alert_state(state: dict[str, Any], path: Path | None = None) -> None:
    target = path or ALERT_STATE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    tmp.replace(target)
