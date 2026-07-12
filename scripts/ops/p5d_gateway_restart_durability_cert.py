#!/usr/bin/env python3
"""
P5D — Gateway restart durability and reconciliation certification.

Reuses the P5C gateway path with independent P5D authorization/counter per scenario.

Usage:
  ./venv/bin/python scripts/ops/p5d_gateway_restart_durability_cert.py --dry-run
  ./venv/bin/python scripts/ops/p5d_gateway_restart_durability_cert.py --refresh-readiness
  ./venv/bin/python scripts/ops/p5d_gateway_restart_durability_cert.py --scenario-a
  ./venv/bin/python scripts/ops/p5d_gateway_restart_durability_cert.py --scenario-b
  ./venv/bin/python scripts/ops/p5d_gateway_restart_durability_cert.py --scenario-c
  ./venv/bin/python scripts/ops/p5d_gateway_restart_durability_cert.py --certify
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_root = Path(__file__).resolve().parents[2]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from sqlalchemy import text
from sqlalchemy.orm import Session

from config.settings import settings
from src.core.database import get_db_context, init_db
from src.core.p4c_send_counter import p4c_live_send_count
from src.core.p5a_send_counter import p5a_live_send_count
from src.core.p5c_send_counter import p5c_live_send_count
from src.core.p5d_authorization import (
    MANIFEST_PATH,
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
)
from src.core.p5d_send_counter import (
    p5d_counter_snapshot,
    p5d_live_send_count,
    record_p5d_live_send,
    reset_p5d_send_counter,
)
from src.core.scheduler_models import (
    AccountTargetBinding,
    ChatTarget,
    JobStatus,
    MessageDelivery,
    MessageTemplate,
    MessageType,
    ScheduledJob,
    SCHEDULED_JOB_P5D_CERTIFICATION_MARKER,
    TemplateScope,
)
from src.recovery.p9_29_v1_readiness_refresh import resolve_database_path
from src.recovery.recovery_lab import sha256_file
from src.scheduler.executor import _create_sending_delivery, _finalize_delivery_sent, _mark_job_failed
from src.telegram_gateway.client import TelegramGatewayClient
from src.telegram_gateway.service import enqueue_job, get_job, reset_stale_running_jobs

PILOT = 107
TARGET_ID = 14
BINDING_ID = 45
PEER_ID = TELEGRAM_PEER_ID
P4C_JOB, P4C_DELIVERY, P4C_TG = 329, 143, 15973
P5A_JOB, P5A_DELIVERY, P5A_TG = 361, 144, 15974
P5C_JOB, P5C_DELIVERY, P5C_TG, P5C_GW = 362, 145, 13, 6064
ENV_PATH = Path("/opt/autostory/.env")
BACKUP_ROOT = Path("data/backups/p5d_gateway_restart_durability")
REPORT_ROOT = Path("data/audit")
SCHEDULER_UNIT = "autostory-scheduler.service"
GATEWAY_UNIT = "telegram-gateway.service"
WEB_UNIT = "autostory-web.service"
TEMPLATE_PREFIX = "P5D Restart Durability"


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


def _systemctl(action: str, unit: str) -> dict[str, Any]:
    r = subprocess.run(["sudo", "systemctl", action, unit], capture_output=True, text=True, timeout=60)
    active = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True)
    return {
        "action": action,
        "unit": unit,
        "ok": r.returncode == 0,
        "active": (active.stdout or "").strip(),
    }


def _read_env_file(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines(keepends=True) if path.is_file() else []


def _set_env_kv(lines: list[str], key: str, value: str) -> list[str]:
    pat = re.compile(rf"^\s*{re.escape(key)}\s*=")
    out, found = [], False
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


def _apply_p5d_env(*, scenario: str, failpoint: str = "", stale_sec: int = 600) -> None:
    os.environ["P5D_CERTIFICATION_MODE"] = "true"
    os.environ["P5D_FAILPOINT"] = failpoint
    os.environ["P5D_SINGLE_SEND_ENABLED"] = "true"
    os.environ["P5D_SINGLE_SEND_MAX"] = "3"
    os.environ["P5C_SINGLE_SEND_ENABLED"] = "false"
    os.environ["SCHEDULER_MUTATIONS_ENABLED"] = "false"
    os.environ["SCHEDULER_MUTATION_SCOPE"] = "send_test_only"
    os.environ["SCHEDULER_MUTATION_ACCOUNT_ALLOWLIST"] = str(PILOT)
    os.environ["TELEGRAM_GATEWAY_RUNNING_STALE_SEC"] = str(stale_sec)
    settings.p5d_single_send_enabled = True
    settings.p5c_single_send_enabled = False
    settings.scheduler_mutations_enabled = False
    settings.scheduler_mutation_scope = "send_test_only"
    settings.scheduler_mutation_account_allowlist = str(PILOT)


def _persist_p5d_env_to_file(*, failpoint: str = "", stale_sec: int = 600) -> Path:
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = BACKUP_ROOT / f"env_before_{stamp}.env"
    if ENV_PATH.is_file():
        shutil.copy2(ENV_PATH, backup)
    lines = _read_env_file(ENV_PATH)
    lines = _set_env_kv(lines, "P5D_CERTIFICATION_MODE", "true")
    lines = _set_env_kv(lines, "P5D_FAILPOINT", failpoint)
    lines = _set_env_kv(lines, "P5D_SINGLE_SEND_ENABLED", "true")
    lines = _set_env_kv(lines, "P5D_SINGLE_SEND_MAX", "3")
    lines = _set_env_kv(lines, "P5C_SINGLE_SEND_ENABLED", "false")
    lines = _set_env_kv(lines, "SCHEDULER_MUTATIONS_ENABLED", "false")
    lines = _set_env_kv(lines, "SCHEDULER_MUTATION_SCOPE", "send_test_only")
    lines = _set_env_kv(lines, "SCHEDULER_MUTATION_ACCOUNT_ALLOWLIST", str(PILOT))
    lines = _set_env_kv(lines, "TELEGRAM_GATEWAY_RUNNING_STALE_SEC", str(stale_sec))
    ENV_PATH.write_text("".join(lines), encoding="utf-8")
    return backup


def _clear_p5d_env_file() -> None:
    lines = _read_env_file(ENV_PATH)
    lines = _remove_env_keys(
        lines,
        {
            "P5D_CERTIFICATION_MODE",
            "P5D_FAILPOINT",
            "P5D_SINGLE_SEND_ENABLED",
            "P5D_SINGLE_SEND_MAX",
            "SCHEDULER_MUTATION_SCOPE",
            "SCHEDULER_MUTATION_ACCOUNT_ALLOWLIST",
        },
    )
    lines = _set_env_kv(lines, "SCHEDULER_MUTATIONS_ENABLED", "false")
    lines = _set_env_kv(lines, "P5C_SINGLE_SEND_ENABLED", "false")
    lines = _set_env_kv(lines, "TELEGRAM_GATEWAY_RUNNING_STALE_SEC", "600")
    ENV_PATH.write_text("".join(lines), encoding="utf-8")


def _clear_p5d_env() -> None:
    for k in (
        "P5D_CERTIFICATION_MODE",
        "P5D_FAILPOINT",
        "P5D_SINGLE_SEND_ENABLED",
        "P5D_SINGLE_SEND_MAX",
        "SCHEDULER_MUTATION_SCOPE",
        "SCHEDULER_MUTATION_ACCOUNT_ALLOWLIST",
    ):
        os.environ.pop(k, None)
    os.environ["TELEGRAM_GATEWAY_RUNNING_STALE_SEC"] = "600"


def _wal_backup(stamp: str) -> dict[str, Any]:
    db_path = resolve_database_path()
    with get_db_context() as db:
        db.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
        db.commit()
        qc = db.execute(text("PRAGMA quick_check")).scalar()
        ic = db.execute(text("PRAGMA integrity_check")).scalar()
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    dest = BACKUP_ROOT / f"storyfleet_p5d_{stamp}.db"
    shutil.copy2(db_path, dest)
    return {
        "backup_path": str(dest.resolve()),
        "backup_sha256": sha256_file(dest),
        "quick_check": qc,
        "integrity_check": ic,
    }


def _queue_snapshot(db: Session) -> dict[str, Any]:
    rows = db.execute(text("SELECT status, COUNT(*) FROM scheduled_jobs GROUP BY status")).fetchall()
    return {
        "scheduled_jobs": {str(r[0]): int(r[1]) for r in rows},
        "pending": int(db.execute(text("SELECT COUNT(*) FROM scheduled_jobs WHERE status='PENDING'")).scalar() or 0),
        "running": int(db.execute(text("SELECT COUNT(*) FROM scheduled_jobs WHERE status='RUNNING'")).scalar() or 0),
        "max_job_id": int(db.execute(text("SELECT COALESCE(MAX(id),0) FROM scheduled_jobs")).scalar() or 0),
        "max_delivery_id": int(db.execute(text("SELECT COALESCE(MAX(id),0) FROM message_deliveries")).scalar() or 0),
        "max_gateway_job_id": int(
            db.execute(text("SELECT COALESCE(MAX(id),0) FROM telegram_gateway_jobs")).scalar() or 0
        ),
    }


def assert_protected_evidence(db: Session) -> dict[str, Any]:
    def _check(job_id, delivery_id, tg_id, label):
        job = db.execute(text("SELECT id,status FROM scheduled_jobs WHERE id=:id"), {"id": job_id}).fetchone()
        deliv = db.execute(
            text("SELECT id,status,tg_message_id FROM message_deliveries WHERE id=:id"),
            {"id": delivery_id},
        ).fetchone()
        ok = (
            job
            and str(job[1]) == "SENT"
            and deliv
            and str(deliv[1]) == "SENT"
            and int(deliv[2]) == int(tg_id)
        )
        return {"ok": ok, "label": label, "job": dict(job._mapping) if job else None, "delivery": dict(deliv._mapping) if deliv else None}

    p4c = _check(P4C_JOB, P4C_DELIVERY, P4C_TG, "p4c")
    p5a = _check(P5A_JOB, P5A_DELIVERY, P5A_TG, "p5a")
    p5c = _check(P5C_JOB, P5C_DELIVERY, P5C_TG, "p5c")
    gw = db.execute(
        text("SELECT id,status,result_json FROM telegram_gateway_jobs WHERE id=:id"),
        {"id": P5C_GW},
    ).fetchone()
    p5c_gw_ok = gw and str(gw[1]) == "done"
    return {
        "ok": p4c["ok"] and p5a["ok"] and p5c["ok"] and p5c_gw_ok,
        "p4c": p4c,
        "p5a": p5a,
        "p5c": p5c,
        "p5c_gateway": dict(gw._mapping) if gw else None,
        "p4c_counter": p4c_live_send_count(),
        "p5a_counter": p5a_live_send_count(),
        "p5c_counter": p5c_live_send_count(),
    }


def _ensure_template(db: Session, *, scenario: str, body: str) -> int:
    name = f"{TEMPLATE_PREFIX} {scenario}"
    binding = db.query(AccountTargetBinding).filter(AccountTargetBinding.id == BINDING_ID).first()
    if not binding:
        raise RuntimeError(f"binding {BINDING_ID} missing")
    tmpl = db.query(MessageTemplate).filter(MessageTemplate.name == name, MessageTemplate.binding_id == BINDING_ID).first()
    if not tmpl:
        tmpl = MessageTemplate(
            name=name,
            type=MessageType.PROMO.value,
            scope=TemplateScope.BINDING.value,
            binding_id=BINDING_ID,
            body=body,
            is_active=True,
            weight=1000,
        )
        db.add(tmpl)
        db.flush()
    else:
        tmpl.body = body
    db.commit()
    return int(tmpl.id)


def _create_job(db: Session) -> int:
    job = ScheduledJob(
        account_id=PILOT,
        target_id=TARGET_ID,
        type=MessageType.PROMO.value,
        run_at=_utc_now_naive(),
        status=JobStatus.PENDING.value,
        last_error=SCHEDULED_JOB_P5D_CERTIFICATION_MARKER,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return int(job.id)


def _prepare_scenario(scenario: str) -> dict[str, Any]:
    body = build_message_body(scenario=scenario)
    manifest = create_manifest(scenario=scenario, message_body=body)
    with get_db_context() as db:
        tmpl_id = _ensure_template(db, scenario=scenario, body=body)
        job_id = _create_job(db)
    set_job_id(job_id)
    manifest = load_manifest() or {}
    manifest["template_id"] = tmpl_id
    save_manifest(manifest)
    return {"scenario": scenario, "job_id": job_id, "message_body": body, "manifest": manifest}


def _reconcile_gateway_result(
    *,
    scenario: str,
    job_id: int,
    delivery_id: int,
    gateway_job_id: int,
    gw_result: dict[str, Any],
) -> dict[str, Any]:
    manifest = load_manifest() or {}
    tg_msg_id = gw_result.get("telegram_message_id")
    send_ok = bool(gw_result.get("ok")) and tg_msg_id is not None
    if send_ok:
        _finalize_delivery_sent(int(delivery_id), int(job_id), int(tg_msg_id), str(manifest.get("message_body") or ""))
        record_p5d_live_send(
            scenario=scenario,
            job_id=int(job_id),
            account_id=PILOT,
            target_id=TARGET_ID,
            authorization_id=str(manifest.get("authorization_id") or ""),
            gateway_job_id=int(gateway_job_id),
            delivery_id=int(delivery_id),
            tg_message_id=int(tg_msg_id),
            reconciled=bool(gw_result.get("reconciled")),
        )
        mark_consumed()
        relock_manifest()
    else:
        _mark_job_failed(int(job_id), f"p5d_gateway_failed:{gw_result.get('error_code')}")
    return {"send_ok": send_ok, "tg_message_id": tg_msg_id, "reconciled": bool(gw_result.get("reconciled"))}


def _wait_gateway(gateway_job_id: int, *, timeout: int = 180) -> dict[str, Any]:
    client = TelegramGatewayClient()
    return client.wait_for_job(int(gateway_job_id), timeout_sec=timeout)


def _run_scenario(scenario: str) -> dict[str, Any]:
    init_db()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    with get_db_context() as db:
        before = assert_protected_evidence(db)
        if not before["ok"]:
            raise RuntimeError("protected evidence check failed before scenario")
        queue_before = _queue_snapshot(db)

    scheduler_stop = _systemctl("stop", SCHEDULER_UNIT)
    gateway_stop = _systemctl("stop", GATEWAY_UNIT)
    time.sleep(2)
    backup = _wal_backup(f"scenario_{scenario}_{stamp}")

    prep = _prepare_scenario(scenario)
    job_id = int(prep["job_id"])
    body = str(prep["message_body"])
    set_armed(True)
    stale = 5 if scenario in ("B", "C") else 600
    _apply_p5d_env(scenario=scenario, stale_sec=stale)
    env_backup = _persist_p5d_env_to_file(failpoint="", stale_sec=stale)

    reserved_ok, reserve_reason = reserve_authorization(job_id=job_id)
    if not reserved_ok:
        raise RuntimeError(f"reserve failed: {reserve_reason}")

    delivery_id = _create_sending_delivery(job_id, PILOT, TARGET_ID, MessageType.PROMO.value, body)
    set_delivery_id(delivery_id)
    payload = {
        "text": body,
        "target_id": TARGET_ID,
        "binding_id": BINDING_ID,
        "job_marker": SCHEDULED_JOB_P5D_CERTIFICATION_MARKER,
        "scheduled_job_id": job_id,
        "delivery_id": delivery_id,
        "authorization_id": (load_manifest() or {}).get("authorization_id"),
        "p5d_certification": True,
        "p5d_scenario": scenario,
    }
    gateway_job_id = enqueue_job(
        account_id=PILOT, task_type="send_message", target=str(PEER_ID), payload=payload
    )
    set_gateway_job_id(gateway_job_id)

    interruption: dict[str, Any] = {"scenario": scenario}
    gw_result: dict[str, Any] = {}

    if scenario == "A":
        _persist_p5d_env_to_file(failpoint="", stale_sec=stale)
        gw_start = _systemctl("restart", GATEWAY_UNIT)
        time.sleep(4)
        gw_result = _wait_gateway(gateway_job_id)
        interruption["mode"] = "restart_before_claim_simulated_via_pending_enqueue"
        interruption["gateway_start"] = gw_start
    elif scenario == "B":
        _persist_p5d_env_to_file(failpoint="p5d_after_gateway_claim", stale_sec=stale)
        gw_start = _systemctl("restart", GATEWAY_UNIT)
        time.sleep(4)
        gw_stop1 = _systemctl("stop", GATEWAY_UNIT)
        _persist_p5d_env_to_file(failpoint="", stale_sec=stale)
        time.sleep(6)
        with get_db_context() as db:
            stale_n = reset_stale_running_jobs(db, older_than_sec=1)
            db.commit()
        gw_start2 = _systemctl("restart", GATEWAY_UNIT)
        time.sleep(4)
        gw_result = _wait_gateway(gateway_job_id, timeout=120)
        interruption.update(
            {"gateway_start_1": gw_start, "gateway_stop": gw_stop1, "stale_recovered": stale_n, "gateway_start_2": gw_start2}
        )
    elif scenario == "C":
        _persist_p5d_env_to_file(failpoint="p5d_after_telegram_send_before_persist", stale_sec=stale)
        gw_start = _systemctl("restart", GATEWAY_UNIT)
        time.sleep(8)
        gw_stop1 = _systemctl("stop", GATEWAY_UNIT)
        _persist_p5d_env_to_file(failpoint="", stale_sec=stale)
        time.sleep(6)
        with get_db_context() as db:
            stale_n = reset_stale_running_jobs(db, older_than_sec=1)
            db.commit()
        gw_start2 = _systemctl("restart", GATEWAY_UNIT)
        time.sleep(4)
        gw_result = _wait_gateway(gateway_job_id, timeout=120)
        interruption.update(
            {"gateway_start_1": gw_start, "gateway_stop": gw_stop1, "stale_recovered": stale_n, "gateway_start_2": gw_start2}
        )
    else:
        raise RuntimeError(f"unknown scenario {scenario}")

    reconcile = _reconcile_gateway_result(
        scenario=scenario,
        job_id=job_id,
        delivery_id=delivery_id,
        gateway_job_id=gateway_job_id,
        gw_result=gw_result,
    )
    _systemctl("stop", GATEWAY_UNIT)
    _systemctl("start", SCHEDULER_UNIT)
    _clear_p5d_env()
    _clear_p5d_env_file()
    _systemctl("restart", WEB_UNIT)

    with get_db_context() as db:
        after = assert_protected_evidence(db)
        queue_after = _queue_snapshot(db)
        deliveries = db.execute(
            text("SELECT id,status,tg_message_id FROM message_deliveries WHERE job_id=:jid"),
            {"jid": job_id},
        ).fetchall()
        gw_row = get_job(gateway_job_id)

    tg_ids = [int(r[2]) for r in deliveries if r[2] is not None]
    sent_rows = [r for r in deliveries if str(r[1]) == "SENT"]
    ok = (
        reconcile["send_ok"]
        and len(sent_rows) == 1
        and len(tg_ids) == 1
        and after["ok"]
        and str(gw_row.status if gw_row else "") == "done"
        and queue_after["pending"] == 0
        and queue_after["running"] == 0
    )

    report = {
        "ok": ok,
        "scenario": scenario,
        "job_id": job_id,
        "gateway_job_id": gateway_job_id,
        "delivery_id": delivery_id,
        "backup": backup,
        "scheduler_stop": scheduler_stop,
        "interruption": interruption,
        "gateway_result": gw_result,
        "reconcile": reconcile,
        "deliveries": [dict(r._mapping) for r in deliveries],
        "gateway_final_status": str(gw_row.status) if gw_row else None,
        "gateway_result_json": gw_row.result_json if gw_row else None,
        "protected_evidence_after": after,
        "queue_before": queue_before,
        "queue_after": queue_after,
        "p5d_counter": p5d_counter_snapshot(),
    }
    path = _write_report(f"p5d_scenario_{scenario.lower()}", report)
    report["report"] = str(path)
    return report


async def _refresh_readiness() -> dict[str, Any]:
    from src.clients.readiness_worker import _deep_check_one

    with get_db_context() as db:
        before = db.execute(
            text(
                "SELECT status, failure_code, checked_at, expires_at FROM account_readiness_snapshots "
                "WHERE account_id=:aid ORDER BY id DESC LIMIT 1"
            ),
            {"aid": PILOT},
        ).fetchone()
    outcome = await _deep_check_one(PILOT)
    with get_db_context() as db:
        after = db.execute(
            text(
                "SELECT status, failure_code, checked_at, expires_at FROM account_readiness_snapshots "
                "WHERE account_id=:aid ORDER BY id DESC LIMIT 1"
            ),
            {"aid": PILOT},
        ).fetchone()
    ok = outcome == "ready" and after and str(after[0]) == "READY"
    return {
        "ok": ok,
        "outcome": outcome,
        "before": dict(before._mapping) if before else None,
        "after": dict(after._mapping) if after else None,
    }


def run_dry_run() -> dict[str, Any]:
    init_db()
    with get_db_context() as db:
        evidence = assert_protected_evidence(db)
        queue = _queue_snapshot(db)
        binding = db.query(AccountTargetBinding).filter(AccountTargetBinding.id == BINDING_ID).first()
        target = db.query(ChatTarget).filter(ChatTarget.id == TARGET_ID).first()
        readiness = db.execute(
            text(
                "SELECT status, expires_at FROM account_readiness_snapshots WHERE account_id=:aid "
                "ORDER BY id DESC LIMIT 1"
            ),
            {"aid": PILOT},
        ).fetchone()
    out = {
        "ok": evidence["ok"],
        "stage": "dry_run",
        "no_mutations": True,
        "readiness": dict(readiness._mapping) if readiness else None,
        "binding_can_post": bool(binding.can_post) if binding else False,
        "target_peer_id": int(target.tg_id) if target and target.tg_id else None,
        "protected_evidence": evidence,
        "queue": queue,
        "scenarios": {
            "A": {"interruption": "gateway restart before claim", "recovery": "single claim on pending job"},
            "B": {"interruption": "failpoint after claim", "recovery": "stale_running_recovered → single send"},
            "C": {
                "interruption": "failpoint after send before persist",
                "recovery": "telegram lookup reconcile, no resend",
            },
        },
        "env": {
            "AUTOSTORY_PRODUCTION_CERTIFIED_NO_GO": os.environ.get("AUTOSTORY_PRODUCTION_CERTIFIED_NO_GO"),
            "PROMO_GENERATION_MODE": os.environ.get("PROMO_GENERATION_MODE"),
            "gateway_active": subprocess.run(["systemctl", "is-active", GATEWAY_UNIT], capture_output=True, text=True).stdout.strip(),
        },
    }
    print(json.dumps(_json_safe(out), indent=2))
    return out


def run_certify() -> dict[str, Any]:
    reset_p5d_send_counter()
    readiness = asyncio.run(_refresh_readiness())
    if not readiness.get("ok"):
        out = {
            "verdict": "P5D_GATEWAY_RESTART_DURABILITY_AND_RECONCILIATION_CERTIFICATION_BLOCKED",
            "blocker": "readiness_refresh_failed",
            "readiness": readiness,
        }
        _write_report("p5d_cert_blocked", out)
        print(json.dumps(out, indent=2))
        return out

    results = {}
    for sc in ("A", "B", "C"):
        results[sc] = _run_scenario(sc)
        if not results[sc].get("ok"):
            out = {
                "verdict": "P5D_GATEWAY_RESTART_DURABILITY_AND_RECONCILIATION_CERTIFICATION_FAIL",
                "failed_scenario": sc,
                "results": results,
                "readiness": readiness,
            }
            _write_report("p5d_cert_fail", out)
            print(json.dumps(out, indent=2))
            return out

    _systemctl("stop", GATEWAY_UNIT)
    _clear_p5d_env_file()

    verdict = "P5D_GATEWAY_RESTART_DURABILITY_AND_RECONCILIATION_CERTIFICATION_PASS"
    out = {
        "verdict": verdict,
        "readiness": readiness,
        "results": results,
        "p5d_counter": p5d_counter_snapshot(),
        "protected_evidence": results["C"]["protected_evidence_after"],
    }
    path = _write_report("p5d_cert_pass", out)
    out["report"] = str(path)
    print(json.dumps(_json_safe(out), indent=2))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="P5D gateway restart durability certification")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--refresh-readiness", action="store_true")
    parser.add_argument("--scenario-a", action="store_true")
    parser.add_argument("--scenario-b", action="store_true")
    parser.add_argument("--scenario-c", action="store_true")
    parser.add_argument("--certify", action="store_true")
    args = parser.parse_args()
    try:
        if args.dry_run:
            return 0 if run_dry_run().get("ok") else 1
        if args.refresh_readiness:
            out = asyncio.run(_refresh_readiness())
            print(json.dumps(_json_safe(out), indent=2))
            return 0 if out.get("ok") else 1
        if args.scenario_a:
            return 0 if _run_scenario("A").get("ok") else 1
        if args.scenario_b:
            return 0 if _run_scenario("B").get("ok") else 1
        if args.scenario_c:
            return 0 if _run_scenario("C").get("ok") else 1
        if args.certify:
            return 0 if run_certify().get("verdict", "").endswith("PASS") else 1
    except RuntimeError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 1
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
