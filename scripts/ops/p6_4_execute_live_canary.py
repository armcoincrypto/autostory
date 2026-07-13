#!/usr/bin/env python3
"""P6.4 live canary execution wrapper.

FAIL-CLOSED. Do not run in P6.4A except against isolated/mock environments.

Requires:
  - fresh successful --execution-preflight artifact
  - P6.2 soak certification PASS
  - operator confirmation token matching expected value
  - max_send_count hard-coded to 1

Does NOT support multi-job, multi-account, batch, campaign, or continuous gateway.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

PILOT_ACCOUNT = 107
TARGET_ID = 1
BINDING_ID = 39
TARGET_TG_ID = 1775722510
MAX_SEND_COUNT = 1
MARKER = "__p6_4_certification__"


def _die(msg: str, code: int = 2) -> None:
    print(json.dumps({"verdict": "P6_4_EXECUTE_BLOCKED", "error": msg}), flush=True)
    raise SystemExit(code)


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _require_p6_2_pass() -> dict[str, Any]:
    base = _REPO / "data/audit/p6_2_soak_baseline_20260713T121009Z.json"
    out = subprocess.run(
        [
            str(_REPO / "venv/bin/python"),
            str(_REPO / "scripts/ops/p6_2_readiness_worker_soak_certify.py"),
            "--baseline",
            str(base),
        ],
        capture_output=True,
        text=True,
        cwd=str(_REPO),
        timeout=180,
    )
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        _die("P6.2 certifier returned non-JSON")
    if out.returncode != 0 or not str(data.get("final_p6_2_verdict", "")).endswith("_PASS"):
        _die(f"P6.2 not PASS: {data.get('final_p6_2_verdict')}")
    return data


def _set_env_key(key: str, value: str) -> None:
    env_path = _REPO / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() else []
    out: list[str] = []
    seen = False
    for line in lines:
        if line.startswith(f"{key}="):
            out.append(f"{key}={value}")
            seen = True
        else:
            out.append(line)
    if not seen:
        out.append(f"{key}={value}")
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.environ[key] = value


def _systemctl(*args: str) -> str:
    r = subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=30)
    return (r.stdout or r.stderr or "").strip()


def main() -> int:
    p = argparse.ArgumentParser(description="P6.4 one-job live canary (fail-closed)")
    p.add_argument("--preflight-artifact", required=True, help="JSON from --execution-preflight")
    p.add_argument("--operator-confirm", required=True, help="Must equal EXPECTED_CONFIRM")
    p.add_argument(
        "--expected-confirm",
        default=os.environ.get("P6_4_OPERATOR_CONFIRM_TOKEN", "P6_4_EXECUTE_ONE_CANARY"),
    )
    p.add_argument("--content-file", default="")
    p.add_argument("--content", default="")
    p.add_argument("--account-id", type=int, default=PILOT_ACCOUNT)
    p.add_argument("--target-id", type=int, default=TARGET_ID)
    p.add_argument("--binding-id", type=int, default=BINDING_ID)
    p.add_argument("--dry-structure-only", action="store_true",
                   help="Validate gates/structure without arming or starting gateway")
    p.add_argument("--out", default="")
    args = p.parse_args()

    if int(args.account_id) != PILOT_ACCOUNT or int(args.target_id) != TARGET_ID or int(args.binding_id) != BINDING_ID:
        _die("account/target/binding must be exact canary triple 107/1/39")
    if args.operator_confirm != args.expected_confirm:
        _die("operator confirmation token mismatch")
    if MAX_SEND_COUNT != 1:
        _die("internal invariant: max_send_count must be 1")

    preflight = _load_json(Path(args.preflight_artifact))
    if preflight.get("mode") != "execution-preflight":
        _die("preflight artifact must be from --execution-preflight")
    if not str(preflight.get("verdict", "")).endswith("_PASS"):
        _die(f"preflight not PASS: {preflight.get('verdict')}")
    if int(preflight.get("account_id") or 0) != PILOT_ACCOUNT:
        _die("preflight account mismatch")
    if int(preflight.get("target_id") or 0) != TARGET_ID:
        _die("preflight target mismatch")
    if int(preflight.get("binding_id") or 0) != BINDING_ID:
        _die("preflight binding mismatch")

    p6_2 = _require_p6_2_pass()

    content = args.content
    if args.content_file:
        content = Path(args.content_file).read_text(encoding="utf-8")
    if not content:
        _die("content required")
    if preflight.get("content_sha256"):
        from src.core.p6_4_authorization import message_sha256

        if message_sha256(content) != preflight["content_sha256"]:
            _die("content hash mismatch vs preflight artifact")

    if args.dry_structure_only:
        report = {
            "verdict": "P6_4_EXECUTE_STRUCTURE_OK_NOT_ARMED",
            "p6_2": p6_2.get("final_p6_2_verdict"),
            "preflight": preflight.get("verdict"),
            "max_send_count": MAX_SEND_COUNT,
            "note": "No gateway start, no authorization arm, no job created",
        }
        print(json.dumps(report, indent=2))
        return 0

    # Live path — still fail-closed until operator explicitly re-runs without structure flag
    # and with P6.2 PASS. P6.4A must not reach here in production.
    from src.core.database import get_db_context, init_db
    from src.core.p6_4_authorization import (
        create_manifest,
        mark_consumed,
        message_sha256,
        relock_manifest,
        reserve_authorization,
        set_armed,
        invalidate_unconsumed,
    )
    from src.core.scheduler_models import (
        JobStatus,
        ScheduledJob,
        SCHEDULED_JOB_P6_4_CERTIFICATION_MARKER,
    )
    from src.telegram_gateway.service import enqueue_job, get_job
    from sqlalchemy import func
    from src.telegram_gateway.models import TelegramGatewayJob

    init_db()
    sha = message_sha256(content)
    evidence: dict[str, Any] = {
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "content_sha256": sha,
        "max_send_count": MAX_SEND_COUNT,
        "p6_2_verdict": p6_2.get("final_p6_2_verdict"),
    }

    with get_db_context() as db:
        pending = (
            db.query(func.count(TelegramGatewayJob.id))
            .filter(TelegramGatewayJob.status.in_(("pending", "retry", "running")))
            .scalar()
            or 0
        )
        if int(pending) > 0:
            _die(f"unrelated gateway backlog={pending}")

    manifest = create_manifest(
        message_body=content,
        story_id=None,
        ttl_minutes=15,
        operator_approval_id=args.operator_confirm,
    )
    evidence["authorization_id"] = manifest["authorization_id"]

    with get_db_context() as db:
        job = ScheduledJob(
            account_id=PILOT_ACCOUNT,
            target_id=TARGET_ID,
            type="PROMO",
            run_at=datetime.utcnow(),
            status=JobStatus.PENDING.value,
            last_error=SCHEDULED_JOB_P6_4_CERTIFICATION_MARKER,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = int(job.id)

    ok, reason = reserve_authorization(job_id=job_id)
    if not ok:
        invalidate_unconsumed()
        _die(f"reserve failed: {reason}")

    gw_job_id = enqueue_job(
        account_id=PILOT_ACCOUNT,
        task_type="send_message",
        target=str(TARGET_TG_ID),
        payload={
            "text": content,
            "expected_message_sha256": sha,
            "target_id": TARGET_ID,
            "binding_id": BINDING_ID,
            "job_marker": MARKER,
            "scheduled_job_id": job_id,
            "p6_4_certification": True,
        },
    )
    evidence["scheduled_job_id"] = job_id
    evidence["gateway_job_id"] = gw_job_id

    # Arm only after exact job + auth reserved
    _set_env_key("P6_4_SINGLE_SEND_ENABLED", "true")
    set_armed(True)

    try:
        _systemctl("start", "telegram-gateway")
        terminal = None
        deadline = time.time() + 120
        while time.time() < deadline:
            row = get_job(int(gw_job_id))
            if row and row.status in ("done", "failed"):
                terminal = {"status": row.status, "result": getattr(row, "result_json", None),
                            "error_code": getattr(row, "error_code", None)}
                break
            time.sleep(1)
        evidence["terminal"] = terminal
        if terminal and terminal.get("status") == "done":
            mark_consumed()
            evidence["verdict"] = "P6_4_LIVE_CANARY_SENT"
        else:
            evidence["verdict"] = "P6_4_LIVE_CANARY_INCOMPLETE"
    finally:
        _systemctl("stop", "telegram-gateway")
        _systemctl("disable", "telegram-gateway")
        _set_env_key("P6_4_SINGLE_SEND_ENABLED", "false")
        _set_env_key("SCHEDULER_MUTATIONS_ENABLED", "false")
        _set_env_key("P5C_SINGLE_SEND_ENABLED", "false")
        _set_env_key("P5D_SINGLE_SEND_ENABLED", "false")
        _set_env_key("AUTOSTORY_PRODUCTION_CERTIFIED_NO_GO", "true")
        relock_manifest()
        evidence["gateway_after"] = {
            "active": _systemctl("is-active", "telegram-gateway"),
            "enabled": _systemctl("is-enabled", "telegram-gateway"),
        }
        evidence["finished_at_utc"] = datetime.now(timezone.utc).isoformat()

    out = args.out or str(
        _REPO
        / "data/audit"
        / f"p6_4_execute_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    )
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(evidence, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps(evidence, indent=2, default=str))
    return 0 if evidence.get("verdict") == "P6_4_LIVE_CANARY_SENT" else 1


if __name__ == "__main__":
    raise SystemExit(main())
