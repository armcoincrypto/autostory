"""P10.11 controlled runtime validation orchestrator."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from src.dashboard.scheduler_mutations import scheduler_mutations_enabled
from src.recovery.p10_fleet_recovery import assert_campaign_completed, assert_safety_locked
from src.recovery.p9_83_governance_observability import build_hash_manifest
from src.recovery.p9_84_soak_gate import run_soak_checks
from src.runtime.ai_runtime_validation import validate_ai_runtime
from src.runtime.discovery_runtime_validation import validate_discovery_runtime
from src.runtime.queue_lifecycle_validation import audit_queue_lifecycle
from src.runtime.runtime_observability import build_runtime_observability_snapshot
from src.runtime.scheduler_runtime_validation import (
    DEFAULT_VALIDATION_ACCOUNTS,
    SchedulerProbeConfig,
    build_scheduler_probe_plan,
)
from src.runtime.stories_runtime_validation import validate_stories_runtime
from src.scheduler.campaign_governance import campaign_execution_enabled

PHASE = "P10.11"
REPORTS_ROOT = Path("data/recovery_lab/reports")


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def json_safe(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj.resolve())
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    return obj


def runtime_safety_preflight(db: Session) -> tuple[list[str], dict[str, Any]]:
    blockers, safety = assert_safety_locked()
    if not safety.get("lock_snapshot", {}).get("locked_ok"):
        blockers.append("locked_ok_false")
    if scheduler_mutations_enabled():
        blockers.append("scheduler_mutations_enabled")
    if campaign_execution_enabled():
        blockers.append("campaign_execution_enabled")
    blockers.extend(assert_campaign_completed(db))
    manifest = build_hash_manifest()
    if not manifest.get("manifest_ok"):
        blockers.append("hash_manifest_not_ok")
    soak = run_soak_checks(db)
    soak_outcome = str(soak.get("outcome") or soak.get("soak_outcome") or "")
    if soak_outcome != "SOAK_STABLE":
        blockers.append("soak_not_stable")
    return blockers, {
        "locked_ok": bool(safety.get("lock_snapshot", {}).get("locked_ok")),
        "soak_stable": soak_outcome == "SOAK_STABLE",
        "soak_outcome": soak_outcome,
        "campaign_completed": not any(str(b).startswith("campaign") for b in blockers),
        "scheduler_mutations_disabled": not scheduler_mutations_enabled(),
        "campaign_execution_disabled": not campaign_execution_enabled(),
        "hash_manifest_ok": bool(manifest.get("manifest_ok")),
        "lock_snapshot": safety.get("lock_snapshot", {}),
    }


def build_runtime_validation_report(
    db: Session,
    *,
    account_ids: list[int] | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    safety_blockers, safety = runtime_safety_preflight(db)
    probe_accounts = tuple(account_ids or list(DEFAULT_VALIDATION_ACCOUNTS))
    scheduler = build_scheduler_probe_plan(
        db,
        SchedulerProbeConfig(accounts=probe_accounts, mode="self-test", max_jobs=2, apply=False),
    )
    stories = validate_stories_runtime(db, account_ids=list(probe_accounts))
    discovery = validate_discovery_runtime(db, account_ids=list(probe_accounts))
    queue = audit_queue_lifecycle(db)
    ai = validate_ai_runtime(db)
    observability = build_runtime_observability_snapshot(db, account_ids=list(probe_accounts))

    track_blockers: list[str] = []
    if safety_blockers:
        track_blockers.extend(safety_blockers)
    if queue.get("blockers"):
        track_blockers.extend([f"queue:{b}" for b in queue["blockers"]])
    if scheduler.get("blockers"):
        track_blockers.append("scheduler_probe_accounts_blocked")

    if safety_blockers:
        outcome = "BLOCKED_WITH_REASON"
    elif track_blockers:
        outcome = "PARTIAL_RUNTIME_VALIDATION_OK"
    else:
        outcome = "CONTROLLED_RUNTIME_VALIDATION_OK"

    return {
        "phase": PHASE,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "apply": apply,
        "outcome": outcome,
        "safety": safety,
        "safety_blockers": safety_blockers,
        "tracks": {
            "A_scheduler": scheduler,
            "B_stories": stories,
            "C_discovery": discovery,
            "D_queue": queue,
            "E_ai_agent_dexpert": ai,
            "F_observability": observability,
        },
        "track_blockers": track_blockers,
        "rollback": [],
        "rollback_note": "No rollback needed; runtime validation report is read-only.",
        "no_mass_send": True,
        "no_public_join": True,
        "no_campaign_execution": True,
        "no_global_unlock": True,
    }


def write_runtime_report(report: dict[str, Any], *, report_stamp: str | None = None) -> dict[str, Any]:
    s = report_stamp or stamp()
    REPORTS_ROOT.mkdir(parents=True, exist_ok=True)
    json_path = REPORTS_ROOT / f"p10_11_runtime_validation_{s}.json"
    md_path = REPORTS_ROOT / f"p10_11_runtime_validation_{s}.md"
    report["report_path"] = str(json_path.resolve())
    report["markdown_path"] = str(md_path.resolve())
    json_path.write_text(json.dumps(json_safe(report), indent=2) + "\n", encoding="utf-8")

    queue = report.get("tracks", {}).get("D_queue", {})
    scheduler = report.get("tracks", {}).get("A_scheduler", {})
    lines = [
        "# P10.11 Controlled Runtime Validation",
        "",
        f"**Outcome:** `{report.get('outcome')}`",
        f"**Generated:** {report.get('generated_at')}",
        "",
        "## Safety",
        "",
        f"- locked_ok: {report.get('safety', {}).get('locked_ok')}",
        f"- soak_stable: {report.get('safety', {}).get('soak_stable')}",
        f"- scheduler_mutations_disabled: {report.get('safety', {}).get('scheduler_mutations_disabled')}",
        f"- campaign_execution_disabled: {report.get('safety', {}).get('campaign_execution_disabled')}",
        "",
        "## Tracks",
        "",
        f"- Scheduler clean accounts: {scheduler.get('clean_accounts')}",
        f"- Scheduler blockers: {len(scheduler.get('blockers') or [])}",
        f"- Queue outcome: {queue.get('outcome')}",
        f"- Queue blockers: {queue.get('blockers')}",
        f"- Active jobs: {queue.get('active_job_count')}",
        "",
        "## Rollback",
        "",
        str(report.get("rollback_note")),
        "",
    ]
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return report
