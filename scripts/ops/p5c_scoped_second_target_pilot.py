#!/usr/bin/env python3
"""
P5C — Scoped second-target single-send lifecycle (account 107 → Saved Messages / target 14).

Gateway claim + send + delivery persistence + reconciliation certification path.

Usage:
  ./venv/bin/python scripts/ops/p5c_scoped_second_target_pilot.py --inspect
  ./venv/bin/python scripts/ops/p5c_scoped_second_target_pilot.py --prepare
  ./venv/bin/python scripts/ops/p5c_scoped_second_target_pilot.py --dry-run
  ./venv/bin/python scripts/ops/p5c_scoped_second_target_pilot.py --arm [--restart-web]
  ./venv/bin/python scripts/ops/p5c_scoped_second_target_pilot.py --execute
  ./venv/bin/python scripts/ops/p5c_scoped_second_target_pilot.py --verify
  ./venv/bin/python scripts/ops/p5c_scoped_second_target_pilot.py --failure-tests
  ./venv/bin/python scripts/ops/p5c_scoped_second_target_pilot.py --relock [--restart-web]
  ./venv/bin/python scripts/ops/p5c_scoped_second_target_pilot.py --status
  ./venv/bin/python scripts/ops/p5c_scoped_second_target_pilot.py --rollback-plan
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

_root = Path(__file__).resolve().parents[2]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from sqlalchemy import text
from sqlalchemy.orm import Session

from config.settings import settings
from src.core.database import get_db_context, init_db
from src.core.execution_guard import (
    ACTION_TELEGRAM_SEND,
    RESULT_ALLOW,
    RESULT_DENY,
    build_execution_lock_matrix,
    can_execute_action,
)
from src.core.p4c_send_counter import p4c_live_send_count, p4c_live_send_remaining
from src.core.p5a_authorization import load_manifest as load_p5a_manifest
from src.core.p5a_send_counter import p5a_live_send_count, p5a_live_send_remaining
from src.core.p5c_authorization import (
    MANIFEST_PATH,
    MARKER,
    TELEGRAM_PEER_ID,
    build_message_body,
    create_manifest,
    load_manifest,
    mark_consumed,
    relock_manifest,
    reserve_authorization,
    save_manifest,
    set_armed,
    set_delivery_id,
    set_gateway_job_id,
    set_job_id,
    validate_p5c_authorization,
)
from src.core.p5c_send_counter import (
    STATE_PATH as P5C_COUNTER_PATH,
    p5c_counter_snapshot,
    p5c_live_send_count,
    p5c_live_send_remaining,
    record_p5c_live_send,
)
from src.core.scheduler_models import (
    AccountTargetBinding,
    ChatTarget,
    JobStatus,
    MessageDelivery,
    MessageTemplate,
    MessageType,
    ScheduledJob,
    SCHEDULED_JOB_P5C_CERTIFICATION_MARKER,
    TemplateScope,
)
from src.dashboard.scheduler_mutations import (
    scoped_send_test_allowlist_active,
    scheduler_mutations_enabled,
)
from src.recovery.p9_29_v1_readiness_refresh import resolve_database_path
from src.recovery.recovery_lab import sha256_file
from src.scheduler.executor import (
    _create_sending_delivery,
    _finalize_delivery_sent,
    _mark_job_failed,
)
from src.telegram_gateway.client import TelegramGatewayClient
from src.telegram_gateway.service import claim_next_jobs, enqueue_job, get_job

PILOT = 107
PROFILE_ID = 97
TARGET_ID = 14
BINDING_ID = 45
PEER_ID = TELEGRAM_PEER_ID
P4C_JOB = 329
P4C_DELIVERY = 143
P4C_TG_MSG_ID = 15973
P5A_JOB = 361
P5A_DELIVERY = 144
P5A_TG_MSG_ID = 15974
PROTECTED_JOB_IDS = {329, 361}
PROTECTED_DELIVERY_IDS = {143, 144}
FORBIDDEN_REUSE_JOBS = {299, 324, 329, 330, 331, 361}
BLOCKED_ACCOUNTS = {139}
TEMPLATE_NAME = "P5C Second Target Test"
ENV_PATH = Path("/opt/autostory/.env")
BACKUP_ROOT = Path("data/backups/p5c_scoped_second_target")
REPORT_ROOT = Path("data/audit")
SCHEDULER_UNIT = "autostory-scheduler.service"
GATEWAY_UNIT = "telegram-gateway.service"
WEB_UNIT = "autostory-web.service"


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _json_safe(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def _write_report(name: str, report: dict[str, Any]) -> Path:
    REPORT_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = REPORT_ROOT / f"{name}_{stamp}.json"
    report["report_path"] = str(path.resolve())
    path.write_text(json.dumps(_json_safe(report), indent=2) + "\n", encoding="utf-8")
    return path


def _read_env_file(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return path.read_text(encoding="utf-8").splitlines(keepends=True)


def _set_env_kv(lines: list[str], key: str, value: str) -> list[str]:
    pat = re.compile(rf"^\s*{re.escape(key)}\s*=")
    out: list[str] = []
    found = False
    for line in lines:
        if pat.match(line):
            out.append(f"{key}={value}\n")
            found = True
        else:
            out.append(line if line.endswith("\n") else line + "\n")
    if not found:
        out.append(f"{key}={value}\n")
    return out


def _remove_env_keys(lines: list[str], keys: set[str]) -> list[str]:
    pat = re.compile(r"^\s*(" + "|".join(re.escape(k) for k in keys) + r")\s*=")
    return [ln for ln in lines if not pat.match(ln)]


def _env_snapshot() -> dict[str, str]:
    keys = (
        "SCHEDULER_MUTATIONS_ENABLED",
        "SCHEDULER_MUTATION_SCOPE",
        "SCHEDULER_MUTATION_ACCOUNT_ALLOWLIST",
        "P4C_SINGLE_SEND_ENABLED",
        "P5A_SINGLE_SEND_ENABLED",
        "P5C_SINGLE_SEND_ENABLED",
        "P5C_SINGLE_SEND_MAX",
        "DISCOVERY_EXECUTION_ENABLED",
        "EXECUTION_EMERGENCY_LOCK",
        "AUTOSTORY_PRODUCTION_CERTIFIED_NO_GO",
    )
    return {k: os.environ.get(k, "") for k in keys}


def reset_p5c_send_counter() -> None:
    if P5C_COUNTER_PATH.is_file():
        P5C_COUNTER_PATH.unlink(missing_ok=True)


def _db_integrity(db) -> dict[str, Any]:
  quick = db.execute(text("PRAGMA quick_check")).scalar()
  integrity = db.execute(text("PRAGMA integrity_check")).scalar()
  return {"quick_check": quick, "integrity_check": integrity}


def _wal_aware_backup(*, stamp: str) -> dict[str, Any]:
    db_path = resolve_database_path()
    if not db_path.is_file():
        raise RuntimeError(f"database file missing: {db_path}")
    with get_db_context() as db:
        db.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
        db.commit()
        integrity_before = _db_integrity(db)
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    dest = BACKUP_ROOT / f"storyfleet_p5c_pre_send_{stamp}.db"
    shutil.copy2(db_path, dest)
    with get_db_context() as db:
        integrity_after = _db_integrity(db)
    return {
        "database_path": str(db_path),
        "backup_path": str(dest.resolve()),
        "backup_sha256": sha256_file(dest),
        "stamp": stamp,
        "integrity_before": integrity_before,
        "integrity_after": integrity_after,
    }


def enable_p5c_scoped_env() -> Path:
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = BACKUP_ROOT / f"env_before_{stamp}.env"
    shutil.copy2(ENV_PATH, backup)
    lines = _read_env_file(ENV_PATH)
    lines = _set_env_kv(lines, "SCHEDULER_MUTATIONS_ENABLED", "false")
    lines = _set_env_kv(lines, "SCHEDULER_MUTATION_SCOPE", "send_test_only")
    lines = _set_env_kv(lines, "SCHEDULER_MUTATION_ACCOUNT_ALLOWLIST", str(PILOT))
    lines = _set_env_kv(lines, "P5C_SINGLE_SEND_ENABLED", "true")
    lines = _set_env_kv(lines, "P5C_SINGLE_SEND_MAX", "1")
    lines = _set_env_kv(lines, "P4C_SINGLE_SEND_ENABLED", "false")
    lines = _set_env_kv(lines, "P5A_SINGLE_SEND_ENABLED", "false")
    lines = _set_env_kv(lines, "DISCOVERY_EXECUTION_ENABLED", "false")
    lines = _set_env_kv(lines, "STORY_EXECUTION_ENABLED", "false")
    lines = _set_env_kv(lines, "CAMPAIGN_EXECUTION_ENABLED", "false")
    lines = _set_env_kv(lines, "EXECUTION_EMERGENCY_LOCK", "false")
    ENV_PATH.write_text("".join(lines), encoding="utf-8")
    return backup


def disable_p5c_scoped_env() -> None:
    lines = _read_env_file(ENV_PATH)
    lines = _set_env_kv(lines, "SCHEDULER_MUTATIONS_ENABLED", "false")
    lines = _remove_env_keys(
        lines,
        {
            "SCHEDULER_MUTATION_ACCOUNT_ALLOWLIST",
            "SCHEDULER_MUTATION_SCOPE",
            "P5C_SINGLE_SEND_ENABLED",
            "P5C_SINGLE_SEND_MAX",
        },
    )
    lines = _set_env_kv(lines, "P4C_SINGLE_SEND_ENABLED", "false")
    lines = _set_env_kv(lines, "P5A_SINGLE_SEND_ENABLED", "false")
    ENV_PATH.write_text("".join(lines), encoding="utf-8")


def _apply_env_to_process() -> None:
    os.environ["SCHEDULER_MUTATIONS_ENABLED"] = "false"
    os.environ["SCHEDULER_MUTATION_SCOPE"] = "send_test_only"
    os.environ["SCHEDULER_MUTATION_ACCOUNT_ALLOWLIST"] = str(PILOT)
    os.environ["P5C_SINGLE_SEND_ENABLED"] = "true"
    os.environ["P5C_SINGLE_SEND_MAX"] = "1"
    os.environ["P4C_SINGLE_SEND_ENABLED"] = "false"
    os.environ["P5A_SINGLE_SEND_ENABLED"] = "false"
    settings.scheduler_mutations_enabled = False
    settings.scheduler_mutation_scope = "send_test_only"
    settings.scheduler_mutation_account_allowlist = str(PILOT)
    settings.p5c_single_send_enabled = True
    settings.p5c_single_send_max = 1
    settings.p4c_single_send_enabled = False
    settings.p5a_single_send_enabled = False


def _clear_env_from_process() -> None:
    for k in (
        "SCHEDULER_MUTATION_ACCOUNT_ALLOWLIST",
        "SCHEDULER_MUTATION_SCOPE",
        "P5C_SINGLE_SEND_ENABLED",
        "P5C_SINGLE_SEND_MAX",
    ):
        os.environ.pop(k, None)
    os.environ["SCHEDULER_MUTATIONS_ENABLED"] = "false"
    settings.scheduler_mutations_enabled = False
    settings.scheduler_mutation_scope = ""
    settings.scheduler_mutation_account_allowlist = ""
    settings.p5c_single_send_enabled = False


def _systemctl(action: str, unit: str) -> dict[str, Any]:
    r = subprocess.run(
        ["sudo", "systemctl", action, unit],
        capture_output=True,
        text=True,
        timeout=60,
    )
    active = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True)
    return {
        "action": action,
        "unit": unit,
        "ok": r.returncode == 0,
        "returncode": r.returncode,
        "active": (active.stdout or "").strip(),
        "stderr": (r.stderr or "")[:300],
    }


def _queue_counts(db: Session) -> dict[str, int]:
    rows = db.execute(text("SELECT status, COUNT(*) FROM scheduled_jobs GROUP BY status")).fetchall()
    return {str(r[0]): int(r[1]) for r in rows}


def assert_queue_clean(db: Session) -> dict[str, Any]:
    queue = _queue_counts(db)
    pending = int(queue.get(JobStatus.PENDING.value, 0))
    running = int(queue.get(JobStatus.RUNNING.value, 0))
    ok = pending == 0 and running == 0
    return {"ok": ok, "queue": queue, "pending": pending, "running": running}


def assert_p4c_protection(db: Session) -> dict[str, Any]:
    job = db.execute(
        text("SELECT id, status, account_id, last_error FROM scheduled_jobs WHERE id=:id"),
        {"id": P4C_JOB},
    ).fetchone()
    delivery = db.execute(
        text(
            "SELECT id, job_id, account_id, status, tg_message_id FROM message_deliveries "
            "WHERE id=:did AND job_id=:jid"
        ),
        {"did": P4C_DELIVERY, "jid": P4C_JOB},
    ).fetchone()
    counter = p4c_live_send_count()
    ok = (
        job is not None
        and str(job[1]) == JobStatus.SENT.value
        and delivery is not None
        and str(delivery[3]) == "SENT"
        and int(delivery[4]) == P4C_TG_MSG_ID
        and counter >= 1
        and p4c_live_send_remaining() == 0
    )
    return {
        "ok": ok,
        "job": dict(job._mapping) if job else None,
        "delivery": dict(delivery._mapping) if delivery else None,
        "p4c_counter": counter,
    }


def assert_p5a_protection(db: Session) -> dict[str, Any]:
    job = db.execute(
        text("SELECT id, status, account_id, last_error FROM scheduled_jobs WHERE id=:id"),
        {"id": P5A_JOB},
    ).fetchone()
    delivery = db.execute(
        text(
            "SELECT id, job_id, account_id, status, tg_message_id FROM message_deliveries "
            "WHERE id=:did AND job_id=:jid"
        ),
        {"did": P5A_DELIVERY, "jid": P5A_JOB},
    ).fetchone()
    counter = p5a_live_send_count()
    manifest = load_p5a_manifest() or {}
    ok = (
        job is not None
        and str(job[1]) == JobStatus.SENT.value
        and delivery is not None
        and str(delivery[3]) == "SENT"
        and int(delivery[4]) == P5A_TG_MSG_ID
        and counter == 1
        and p5a_live_send_remaining() == 0
        and manifest.get("consumed") is True
        and not manifest.get("armed")
    )
    return {
        "ok": ok,
        "job": dict(job._mapping) if job else None,
        "delivery": dict(delivery._mapping) if delivery else None,
        "p5a_counter": counter,
        "p5a_manifest_consumed": manifest.get("consumed"),
    }


def _forbidden_job_check(db: Session) -> dict[str, Any]:
    ids_csv = ",".join(str(i) for i in sorted(FORBIDDEN_REUSE_JOBS))
    rows = db.execute(
        text(
            f"""
            SELECT id, status, account_id, target_id, last_error
            FROM scheduled_jobs WHERE id IN ({ids_csv}) ORDER BY id
            """
        )
    ).fetchall()
    active_forbidden = [
        dict(r._mapping)
        for r in rows
        if str(r[1]) in (JobStatus.PENDING.value, JobStatus.RUNNING.value)
    ]
    marker_active = db.execute(
        text(
            """
            SELECT id, status, account_id, target_id, last_error
            FROM scheduled_jobs
            WHERE last_error = :marker AND status IN ('PENDING', 'RUNNING')
            """
        ),
        {"marker": SCHEDULED_JOB_P5C_CERTIFICATION_MARKER},
    ).fetchall()
    return {
        "forbidden_jobs": [dict(r._mapping) for r in rows],
        "active_forbidden": active_forbidden,
        "p5c_marker_active": [dict(r._mapping) for r in marker_active],
        "ok": not active_forbidden and not marker_active,
    }


def _phase_a_freshness(db: Session) -> dict[str, Any]:
    binding = db.query(AccountTargetBinding).filter(AccountTargetBinding.id == BINDING_ID).first()
    target = db.query(ChatTarget).filter(ChatTarget.id == TARGET_ID).first()
    profile = db.execute(
        text("SELECT id, account_id, is_enabled, timezone FROM schedule_profiles WHERE id=:id"),
        {"id": PROFILE_ID},
    ).fetchone()
    readiness = db.execute(
        text(
            "SELECT status, failure_code, expires_at FROM account_readiness_snapshots "
            "WHERE account_id=:aid ORDER BY id DESC LIMIT 1"
        ),
        {"aid": PILOT},
    ).fetchone()
    manifest = load_manifest()
    blockers: list[str] = []
    if not binding or int(binding.account_id) != PILOT or int(binding.target_id) != TARGET_ID:
        blockers.append("binding_invalid")
    elif not binding.can_post:
        blockers.append("binding_can_post_false")
    if not target or int(target.tg_id or 0) != PEER_ID:
        blockers.append("target_identity_mismatch")
    if not profile or int(profile[1]) != PILOT:
        blockers.append("profile_invalid")
    if readiness is None or str(readiness[0]) != "READY":
        blockers.append("readiness_not_ready")
    ok_auth, reason, _ = validate_p5c_authorization(
        account_id=PILOT,
        target_id=TARGET_ID,
        job_marker=MARKER,
        require_armed=False,
    )
    if not ok_auth and reason not in ("p5c_scope_inactive",):
        blockers.append(f"authorization:{reason}")
    if manifest and not manifest.get("operator_approved"):
        blockers.append("operator_approval_missing")
    return {
        "ok": not blockers,
        "blockers": blockers,
        "binding_id": BINDING_ID,
        "target_id": TARGET_ID,
        "peer_id": PEER_ID,
        "profile": dict(profile._mapping) if profile else None,
        "readiness": dict(readiness._mapping) if readiness else None,
        "binding_can_post": bool(binding.can_post) if binding else False,
        "operator_approved": bool((manifest or {}).get("operator_approved")),
    }


def ensure_p5c_template(db: Session, *, message_body: str) -> dict[str, Any]:
    binding = db.query(AccountTargetBinding).filter(AccountTargetBinding.id == BINDING_ID).first()
    if not binding or int(binding.account_id) != PILOT or int(binding.target_id) != TARGET_ID:
        raise RuntimeError(f"binding {BINDING_ID} missing or not {PILOT}→target {TARGET_ID}")
    tmpl = (
        db.query(MessageTemplate)
        .filter(MessageTemplate.name == TEMPLATE_NAME, MessageTemplate.binding_id == BINDING_ID)
        .first()
    )
    created = False
    if not tmpl:
        tmpl = MessageTemplate(
            name=TEMPLATE_NAME,
            type=MessageType.PROMO.value,
            scope=TemplateScope.BINDING.value,
            binding_id=BINDING_ID,
            body=message_body,
            is_active=True,
            weight=1000,
        )
        db.add(tmpl)
        db.flush()
        created = True
    else:
        tmpl.body = message_body
        tmpl.is_active = True
    db.commit()
    return {"template_id": int(tmpl.id), "created": created, "body": message_body}


def create_p5c_job(db: Session) -> dict[str, Any]:
    target = db.query(ChatTarget).filter(ChatTarget.id == TARGET_ID).first()
    if not target or int(target.tg_id or 0) != PEER_ID:
        raise RuntimeError(f"target {TARGET_ID} peer mismatch (expected {PEER_ID})")
    now = _utc_now_naive()
    job = ScheduledJob(
        account_id=PILOT,
        target_id=TARGET_ID,
        type=MessageType.PROMO.value,
        run_at=now,
        status=JobStatus.PENDING.value,
        last_error=SCHEDULED_JOB_P5C_CERTIFICATION_MARKER,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    if int(job.id) in FORBIDDEN_REUSE_JOBS:
        raise RuntimeError(f"fresh job id {job.id} collides with forbidden reuse set")
    return {
        "job_id": int(job.id),
        "account_id": PILOT,
        "target_id": TARGET_ID,
        "peer_id": PEER_ID,
        "binding_id": BINDING_ID,
        "marker": SCHEDULED_JOB_P5C_CERTIFICATION_MARKER,
    }


def dry_run_p5c_job(db: Session, job_id: int) -> dict[str, Any]:
    os.environ["P5C_SINGLE_SEND_ENABLED"] = "false"
    settings.p5c_single_send_enabled = False
    denied_139 = can_execute_action(
        ACTION_TELEGRAM_SEND,
        account_id=139,
        target_id=TARGET_ID,
        job_marker=SCHEDULED_JOB_P5C_CERTIFICATION_MARKER,
        skip_audit=True,
    )
    dry = can_execute_action(
        ACTION_TELEGRAM_SEND,
        account_id=PILOT,
        target_id=TARGET_ID,
        dry_run=True,
        job_marker=SCHEDULED_JOB_P5C_CERTIFICATION_MARKER,
        skip_audit=True,
    )
    live_blocked = can_execute_action(
        ACTION_TELEGRAM_SEND,
        account_id=PILOT,
        target_id=TARGET_ID,
        dry_run=False,
        db=db,
        job_marker=SCHEDULED_JOB_P5C_CERTIFICATION_MARKER,
        job_id=int(job_id),
        skip_audit=True,
    )
    job = db.query(ScheduledJob).filter(ScheduledJob.id == int(job_id)).first()
    delivery_count = db.query(MessageDelivery).filter(MessageDelivery.job_id == int(job_id)).count()
    gw_count = db.execute(
        text("SELECT COUNT(*) FROM telegram_gateway_jobs WHERE account_id=:aid AND status IN ('pending','running')"),
        {"aid": PILOT},
    ).scalar()
    os.environ["P5C_SINGLE_SEND_ENABLED"] = "true"
    settings.p5c_single_send_enabled = True
    manifest = load_manifest()
    armed_probe = can_execute_action(
        ACTION_TELEGRAM_SEND,
        account_id=PILOT,
        target_id=TARGET_ID,
        dry_run=False,
        db=db,
        job_marker=SCHEDULED_JOB_P5C_CERTIFICATION_MARKER,
        job_id=int(job_id),
        skip_audit=True,
    )
    os.environ["P5C_SINGLE_SEND_ENABLED"] = "false"
    settings.p5c_single_send_enabled = False
    armed_allow = (
        bool(manifest and manifest.get("armed"))
        and armed_probe.result == RESULT_ALLOW
        and armed_probe.reason_code == "p5c_scoped_second_target_authorized"
    )
    ok = (
        denied_139.result == RESULT_DENY
        and dry.result == RESULT_ALLOW
        and live_blocked.result == RESULT_DENY
        and live_blocked.reason_code == "p5c_scope_inactive"
        and scoped_send_test_allowlist_active()
        and not scheduler_mutations_enabled()
        and job is not None
        and str(job.status) == JobStatus.PENDING.value
        and delivery_count == 0
        and int(gw_count or 0) == 0
        and armed_allow
    )
    return {
        "ok": ok,
        "denied_account_139": denied_139.to_dict(),
        "dry_run_decision": dry.to_dict(),
        "live_guard_blocked": live_blocked.to_dict(),
        "armed_scope_probe": armed_probe.to_dict(),
        "delivery_count": delivery_count,
        "gateway_pending_running": int(gw_count or 0),
        "p5c_remaining": p5c_live_send_remaining(),
    }


def _test_duplicate_claim_denied(db) -> dict[str, Any]:
    """Enqueue a throwaway gateway job and prove second claim cannot steal running row."""
    gid = enqueue_job(
        account_id=PILOT,
        task_type="send_message",
        target=str(PEER_ID),
        payload={"text": "P5C duplicate-claim probe — must not send", "probe_only": True},
    )
    first: list[Any] = []
    second: list[Any] = []
    with get_db_context() as db2:
        first = claim_next_jobs(db2, limit=1)
        db2.commit()
    with get_db_context() as db2:
        second = claim_next_jobs(db2, limit=1)
        db2.commit()
    with get_db_context() as db2:
        row = db2.get(
            __import__("src.telegram_gateway.models", fromlist=["TelegramGatewayJob"]).TelegramGatewayJob,
            int(gid),
        )
        status = str(row.status) if row else None
        db2.execute(
            text("UPDATE telegram_gateway_jobs SET status='failed', error_code='p5c_probe_cleanup' WHERE id=:id"),
            {"id": gid},
        )
        db2.commit()
    ok = len(first) == 1 and int(first[0].id) == int(gid) and len(second) == 0 and status == "running"
    return {
        "ok": ok,
        "gateway_job_id": gid,
        "first_claim_count": len(first),
        "second_claim_count": len(second),
        "status_after_first_claim": status,
    }


def run_failure_tests() -> dict[str, Any]:
    init_db()
    manifest = load_manifest()
    if not manifest:
        raise RuntimeError("No manifest — run --prepare and --arm first")
    job_id = int(manifest.get("job_id") or 0)
    tests: dict[str, Any] = {}

    m = dict(manifest)
    m["expires_at_utc"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    save_manifest(m)
    ok, reason, _ = validate_p5c_authorization(
        account_id=PILOT, target_id=TARGET_ID, job_marker=MARKER, require_armed=True
    )
    tests["expired_authorization"] = {"ok": not ok and reason == "p5c_authorization_expired", "reason": reason}
    save_manifest(manifest)

    m2 = load_manifest() or {}
    m2["consumed"] = True
    save_manifest(m2)
    ok2, reason2, _ = validate_p5c_authorization(
        account_id=PILOT, target_id=TARGET_ID, job_marker=MARKER, require_armed=True
    )
    tests["consumed_authorization"] = {"ok": not ok2 and reason2 == "p5c_authorization_consumed", "reason": reason2}
    m2["consumed"] = False
    save_manifest(m2)

    _apply_env_to_process()
    d = can_execute_action(
        ACTION_TELEGRAM_SEND,
        account_id=PILOT,
        target_id=999,
        job_marker=SCHEDULED_JOB_P5C_CERTIFICATION_MARKER,
        job_id=job_id,
        skip_audit=True,
    )
    tests["stale_target_permission"] = {
        "ok": d.result == RESULT_DENY and d.reason_code == "p5c_target_mismatch",
        "decision": d.to_dict(),
    }

    with get_db_context() as db:
        tests["duplicate_claim_denied"] = _test_duplicate_claim_denied(db)

    tests["all_ok"] = all(t.get("ok") for t in tests.values() if isinstance(t, dict))
    out = {"stage": "failure_tests", "tests": tests}
    path = _write_report("p5c_failure_tests", out)
    out["report"] = str(path)
    print(json.dumps(_json_safe(out), indent=2))
    return out


def run_inspect() -> dict[str, Any]:
    init_db()
    with get_db_context() as db:
        queue = assert_queue_clean(db)
        p4c = assert_p4c_protection(db)
        p5a = assert_p5a_protection(db)
        forbidden = _forbidden_job_check(db)
        freshness = _phase_a_freshness(db)
    manifest = load_manifest()
    out = {
        "stage": "inspect",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "queue": queue,
        "p4c_protection": p4c,
        "p5a_protection": p5a,
        "forbidden_jobs": forbidden,
        "phase_a_freshness": freshness,
        "manifest": manifest,
        "p5c_counter": p5c_counter_snapshot(),
        "env": _env_snapshot(),
        "lock_matrix": build_execution_lock_matrix(),
        "ready_for_prepare": queue["ok"] and p4c["ok"] and p5a["ok"] and forbidden["ok"] and freshness["ok"],
    }
    print(json.dumps(_json_safe(out), indent=2))
    return out


def run_prepare() -> dict[str, Any]:
    init_db()
    reset_p5c_send_counter()
    with get_db_context() as db:
        queue = assert_queue_clean(db)
        if not queue["ok"]:
            raise RuntimeError(f"queue not clean: pending={queue['pending']} running={queue['running']}")
        p4c = assert_p4c_protection(db)
        p5a = assert_p5a_protection(db)
        if not p4c["ok"] or not p5a["ok"]:
            raise RuntimeError("P4C/P5A protection check failed")
        forbidden = _forbidden_job_check(db)
        if not forbidden["ok"]:
            raise RuntimeError("forbidden reuse or active P5C marker jobs present")
        freshness = _phase_a_freshness(db)
        if not freshness["ok"]:
            raise RuntimeError(f"phase A freshness failed: {freshness['blockers']}")

        message_body = build_message_body()
        manifest = create_manifest(
            message_body=message_body,
            operator_approved=True,
        )
        manifest["operator_approval_note"] = (
            "Explicit P5C certification scope: account 107, target 14, peer 8000295303, binding 45"
        )
        manifest["p4c_baseline_counter"] = p4c["p4c_counter"]
        manifest["p5a_baseline_counter"] = p5a["p5a_counter"]
        manifest["prepared_at_utc"] = datetime.now(timezone.utc).isoformat()
        save_manifest(manifest)
        tmpl_out = ensure_p5c_template(db, message_body=message_body)
        job_out = create_p5c_job(db)
        set_job_id(int(job_out["job_id"]))

    out = {
        "ok": True,
        "stage": "prepare",
        "manifest": load_manifest(),
        "template": tmpl_out,
        "job": job_out,
        "freshness": freshness,
    }
    path = _write_report("p5c_prepare", out)
    out["report"] = str(path)
    print(json.dumps(_json_safe(out), indent=2))
    return out


def run_dry_run() -> dict[str, Any]:
    init_db()
    manifest = load_manifest()
    if not manifest or not manifest.get("job_id"):
        raise RuntimeError("No prepared manifest/job_id — run --prepare first")
    if not manifest.get("armed"):
        raise RuntimeError("Manifest not armed — run --arm first")
    _apply_env_to_process()
    job_id = int(manifest["job_id"])
    with get_db_context() as db:
        dry_out = dry_run_p5c_job(db, job_id)
    out = {"ok": bool(dry_out.get("ok")), "stage": "dry_run", "job_id": job_id, "dry_run": dry_out}
    path = _write_report("p5c_dry_run", out)
    out["report"] = str(path)
    print(json.dumps(_json_safe(out), indent=2))
    return out


def run_arm(*, restart_web: bool = False) -> dict[str, Any]:
    manifest = load_manifest()
    if not manifest:
        raise RuntimeError("No P5C manifest — run --prepare first")
    if manifest.get("consumed"):
        raise RuntimeError("Manifest already consumed")
    env_backup = enable_p5c_scoped_env()
    _apply_env_to_process()
    set_armed(True)
    web_restart = _systemctl("restart", WEB_UNIT) if restart_web else None
    if restart_web:
        time.sleep(3)
    out = {
        "ok": True,
        "stage": "arm",
        "armed": True,
        "env_backup": str(env_backup),
        "web_restart": web_restart,
        "manifest": load_manifest(),
        "env": _env_snapshot(),
    }
    path = _write_report("p5c_arm", out)
    out["report"] = str(path)
    print(json.dumps(_json_safe(out), indent=2))
    return out


def run_execute() -> dict[str, Any]:
    init_db()
    manifest = load_manifest()
    if not manifest or not manifest.get("armed"):
        raise RuntimeError("Manifest not armed — run --arm first")
    if manifest.get("consumed"):
        raise RuntimeError("Manifest already consumed")
    job_id = int(manifest.get("job_id") or 0)
    message_body = str(manifest.get("message_body") or "")
    if not job_id or not message_body:
        raise RuntimeError("Manifest missing job_id or message_body")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    env_before = _env_snapshot()
    p4c_before = p4c_live_send_count()
    p5a_before = p5a_live_send_count()
    with get_db_context() as db:
        max_gw_before = int(
            db.execute(text("SELECT COALESCE(MAX(id),0) FROM telegram_gateway_jobs")).scalar() or 0
        )

    scheduler_stop = _systemctl("stop", SCHEDULER_UNIT)
    gateway_stop = _systemctl("stop", GATEWAY_UNIT)
    time.sleep(2)

    db_backup = _wal_aware_backup(stamp=stamp)

    reserved_ok, reserve_reason = reserve_authorization(job_id=job_id)
    if not reserved_ok:
        _systemctl("start", GATEWAY_UNIT)
        _systemctl("start", SCHEDULER_UNIT)
        raise RuntimeError(f"reserve_authorization failed: {reserve_reason}")

    _apply_env_to_process()
    delivery_id = _create_sending_delivery(
        job_id, PILOT, TARGET_ID, MessageType.PROMO.value, message_body
    )
    set_delivery_id(delivery_id)

    payload = {
        "text": message_body,
        "target_id": TARGET_ID,
        "binding_id": BINDING_ID,
        "job_marker": SCHEDULED_JOB_P5C_CERTIFICATION_MARKER,
        "scheduled_job_id": job_id,
        "delivery_id": delivery_id,
        "authorization_id": manifest.get("authorization_id"),
        "p5c_certification": True,
    }
    gateway_job_id = enqueue_job(
        account_id=PILOT,
        task_type="send_message",
        target=str(PEER_ID),
        payload=payload,
    )
    set_gateway_job_id(gateway_job_id)

    gateway_start = _systemctl("start", GATEWAY_UNIT)
    time.sleep(3)

    client = TelegramGatewayClient()
    gw_result = client.wait_for_job(int(gateway_job_id), timeout_sec=180)

    with get_db_context() as db:
        gw_row = get_job(int(gateway_job_id))
        claimed_once = gw_row is not None and str(gw_row.status) in ("done", "failed", "running")
        gw_status = str(gw_row.status) if gw_row else None
        duplicate_claim = _test_duplicate_claim_denied(db)

    send_ok = bool(gw_result.get("ok")) and gw_result.get("telegram_message_id") is not None
    tg_msg_id = int(gw_result["telegram_message_id"]) if send_ok else None

    if send_ok and tg_msg_id is not None:
        _finalize_delivery_sent(delivery_id, job_id, tg_msg_id, message_body)
        record_p5c_live_send(
            job_id=job_id,
            account_id=PILOT,
            target_id=TARGET_ID,
            binding_id=BINDING_ID,
            authorization_id=str(manifest.get("authorization_id") or ""),
            gateway_job_id=gateway_job_id,
            delivery_id=delivery_id,
            tg_message_id=tg_msg_id,
        )
        mark_consumed()
    else:
        _mark_job_failed(job_id, f"gateway_send_failed:{gw_result.get('error_code')}")

    gateway_stop_after = _systemctl("stop", GATEWAY_UNIT)
    scheduler_start = _systemctl("start", SCHEDULER_UNIT)

    with get_db_context() as db:
        job = db.query(ScheduledJob).filter(ScheduledJob.id == job_id).first()
        deliveries = (
            db.query(MessageDelivery).filter(MessageDelivery.job_id == job_id).order_by(MessageDelivery.id).all()
        )
        delivery_rows = [
            {
                "id": int(d.id),
                "job_id": int(d.job_id),
                "account_id": int(d.account_id),
                "target_id": int(d.target_id),
                "status": d.status,
                "tg_message_id": d.tg_message_id,
            }
            for d in deliveries
        ]
        after_counts = _queue_counts(db)
        integrity = _db_integrity(db)
        p4c_after = assert_p4c_protection(db)
        p5a_after = assert_p5a_protection(db)

    p5c_count = p5c_live_send_count()
    sent_deliveries = [r for r in delivery_rows if str(r.get("status")) == "SENT"]
    ok = (
        send_ok
        and job is not None
        and str(job.status) == JobStatus.SENT.value
        and len(sent_deliveries) == 1
        and sent_deliveries[0].get("tg_message_id") == tg_msg_id
        and p5c_count == 1
        and p4c_live_send_count() == p4c_before
        and p5a_live_send_count() == p5a_before
        and int(gateway_job_id) > max_gw_before
        and gw_status == "done"
        and duplicate_claim.get("ok")
        and p4c_after["ok"]
        and p5a_after["ok"]
        and after_counts.get("PENDING", 0) == 0
        and after_counts.get("RUNNING", 0) == 0
    )

    report = {
        "ok": ok,
        "outcome": "P5C_SCOPED_SECOND_TARGET_EXECUTE_PASS" if ok else "P5C_SCOPED_SECOND_TARGET_EXECUTE_FAIL",
        "stage": "execute",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "account_id": PILOT,
        "target_id": TARGET_ID,
        "binding_id": BINDING_ID,
        "job_id": job_id,
        "gateway_job_id": gateway_job_id,
        "delivery_id": delivery_id,
        "authorization_id": manifest.get("authorization_id"),
        "tg_message_id": tg_msg_id,
        "db_backup": db_backup,
        "scheduler_stop": scheduler_stop,
        "gateway_start": gateway_start,
        "gateway_stop_after": gateway_stop_after,
        "scheduler_start": scheduler_start,
        "reserve": {"ok": reserved_ok, "reason": reserve_reason},
        "gateway_result": gw_result,
        "gateway_status": gw_status,
        "duplicate_claim_test": duplicate_claim,
        "deliveries": delivery_rows,
        "p5c_live_send_count": p5c_count,
        "p4c_before": p4c_before,
        "p5a_before": p5a_before,
        "queue_after": after_counts,
        "integrity_after": integrity,
        "p4c_protection": p4c_after,
        "p5a_protection": p5a_after,
        "env_before": env_before,
        "never_retried": True,
    }
    path = _write_report("p5c_scoped_second_target_execute", report)
    report["report_written"] = str(path)
    print(json.dumps(_json_safe({"outcome": report["outcome"], "ok": ok, "report": str(path)}), indent=2))
    return report


def run_verify() -> dict[str, Any]:
    init_db()
    manifest = load_manifest() or {}
    job_id = int(manifest.get("job_id") or 0)
    blockers: list[str] = []
    with get_db_context() as db:
        queue = assert_queue_clean(db)
        p4c = assert_p4c_protection(db)
        p5a = assert_p5a_protection(db)
        job_row = None
        delivery_rows: list[dict[str, Any]] = []
        if job_id:
            job = db.query(ScheduledJob).filter(ScheduledJob.id == job_id).first()
            if job:
                job_row = {
                    "id": int(job.id),
                    "status": job.status,
                    "account_id": int(job.account_id),
                    "target_id": int(job.target_id),
                }
            deliveries = (
                db.query(MessageDelivery).filter(MessageDelivery.job_id == job_id).order_by(MessageDelivery.id).all()
            )
            delivery_rows = [
                {"id": int(d.id), "status": d.status, "tg_message_id": d.tg_message_id, "target_id": int(d.target_id)}
                for d in deliveries
            ]
    sent = [d for d in delivery_rows if str(d.get("status")) == "SENT"]
    if not job_row or str(job_row.get("status")) != JobStatus.SENT.value:
        blockers.append("job_not_sent")
    if len(sent) != 1:
        blockers.append(f"delivery_count_not_one:{len(sent)}")
    if int(sent[0].get("target_id") if sent else 0) != TARGET_ID:
        blockers.append("wrong_target")
    if p5c_live_send_count() != 1:
        blockers.append("p5c_counter_not_one")
    if not manifest.get("consumed"):
        blockers.append("manifest_not_consumed")
    if manifest.get("armed"):
        blockers.append("manifest_still_armed")
    if not p4c["ok"] or not p5a["ok"]:
        blockers.append("prior_cert_evidence_changed")
    if not queue["ok"]:
        blockers.append("queue_not_clean")
    ok = not blockers
    out = {
        "ok": ok,
        "verdict": "P5C_SCOPED_SECOND_TARGET_PASS" if ok else "P5C_SCOPED_SECOND_TARGET_FAIL",
        "blockers": blockers,
        "job": job_row,
        "deliveries": delivery_rows,
        "p5c_counter": p5c_counter_snapshot(),
        "manifest": manifest,
    }
    print(json.dumps(_json_safe(out), indent=2))
    return out


def run_relock(*, restart_web: bool = False) -> dict[str, Any]:
    disable_p5c_scoped_env()
    _clear_env_from_process()
    relock_manifest()
    gateway_stop = _systemctl("stop", GATEWAY_UNIT)
    web_restart = _systemctl("restart", WEB_UNIT) if restart_web else None
    if restart_web:
        time.sleep(3)
    out = {
        "ok": True,
        "stage": "relock",
        "gateway_stop": gateway_stop,
        "web_restart": web_restart,
        "manifest": load_manifest(),
        "env": _env_snapshot(),
    }
    path = _write_report("p5c_relock", out)
    out["report"] = str(path)
    print(json.dumps(_json_safe(out), indent=2))
    return out


def run_status() -> dict[str, Any]:
    init_db()
    manifest = load_manifest()
    with get_db_context() as db:
        queue = _queue_counts(db)
        p4c = assert_p4c_protection(db)
        p5a = assert_p5a_protection(db)
    job_id = int((manifest or {}).get("job_id") or 0)
    out = {
        "phase": "P5C",
        "manifest_present": manifest is not None,
        "authorization_id": (manifest or {}).get("authorization_id"),
        "armed": bool((manifest or {}).get("armed")),
        "consumed": bool((manifest or {}).get("consumed")),
        "job_id": job_id or None,
        "gateway_job_id": (manifest or {}).get("gateway_job_id"),
        "p5c_counter": p5c_counter_snapshot(),
        "p4c_protection_ok": p4c.get("ok"),
        "p5a_protection_ok": p5a.get("ok"),
        "queue": queue,
        "env": _env_snapshot(),
    }
    print(json.dumps(_json_safe(out), indent=2))
    return out


def rollback_plan() -> None:
    db_backups = sorted(BACKUP_ROOT.glob("storyfleet_p5c_pre_send_*.db"))
    env_backups = sorted(BACKUP_ROOT.glob("env_before_*.env"))
    latest_db = db_backups[-1] if db_backups else None
    latest_env = env_backups[-1] if env_backups else None
    print(
        json.dumps(
            {
                "db_backup": str(latest_db) if latest_db else None,
                "env_backup": str(latest_env) if latest_env else None,
                "manifest": str(MANIFEST_PATH) if MANIFEST_PATH.is_file() else None,
                "p5c_counter": str(P5C_COUNTER_PATH) if P5C_COUNTER_PATH.is_file() else None,
            },
            indent=2,
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="P5C scoped second-target lifecycle pilot")
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--arm", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--failure-tests", action="store_true")
    parser.add_argument("--relock", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--rollback-plan", action="store_true")
    parser.add_argument("--restart-web", action="store_true")
    args = parser.parse_args()
    try:
        if args.inspect:
            run_inspect()
            return 0
        if args.prepare:
            run_prepare()
            return 0
        if args.dry_run:
            return 0 if run_dry_run().get("ok") else 1
        if args.arm:
            run_arm(restart_web=args.restart_web)
            return 0
        if args.execute:
            return 0 if run_execute().get("ok") else 1
        if args.verify:
            return 0 if run_verify().get("ok") else 1
        if args.failure_tests:
            return 0 if run_failure_tests().get("tests", {}).get("all_ok") else 1
        if args.relock:
            run_relock(restart_web=args.restart_web)
            return 0
        if args.status:
            run_status()
            return 0
        if args.rollback_plan:
            rollback_plan()
            return 0
    except RuntimeError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 1
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
