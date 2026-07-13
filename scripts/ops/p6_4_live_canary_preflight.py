#!/usr/bin/env python3
"""P6.4 live canary preflight — read-only by default.

Modes:
  --dry-run              Inspect production truth; never mutate. Allowed before P6.2 PASS.
  --execution-preflight  Refuses unless P6.2 soak certification is PASS.

Does not send Telegram messages. Does not start the gateway.
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

DEFAULT_ACCOUNT = 107
DEFAULT_TARGET = 1
DEFAULT_BINDING = 39
BASELINE = _REPO / "data/audit/p6_2_soak_baseline_20260713T121009Z.json"


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _svc(name: str) -> dict[str, str]:
    def _run(cmd: list[str]) -> str:
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
            return (out.stdout or "").strip() or "unknown"
        except Exception:
            return "unknown"

    return {"active": _run(["systemctl", "is-active", name]), "enabled": _run(["systemctl", "is-enabled", name])}


def _env_flag(key: str) -> str:
    if key in os.environ and str(os.environ.get(key) or "").strip():
        return str(os.environ.get(key)).strip()
    env = _REPO / ".env"
    if env.is_file():
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip().startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    return ""


def _p6_2_result() -> dict[str, Any]:
    if not BASELINE.is_file():
        return {"ok": False, "verdict": "MISSING_BASELINE", "exit_code": 2}
    out = subprocess.run(
        [
            str(_REPO / "venv/bin/python"),
            str(_REPO / "scripts/ops/p6_2_readiness_worker_soak_certify.py"),
            "--baseline",
            str(BASELINE),
        ],
        capture_output=True,
        text=True,
        cwd=str(_REPO),
        timeout=120,
    )
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        data = {"raw_stdout": (out.stdout or "")[:2000], "stderr": (out.stderr or "")[:1000]}
    data["exit_code"] = out.returncode
    data["ok"] = out.returncode == 0 and data.get("final_p6_2_verdict", "").endswith("_PASS")
    return data


def _content_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def run_preflight(
    *,
    account_id: int,
    target_id: int,
    binding_id: int,
    content: str,
    execution_mode: bool,
) -> dict[str, Any]:
    from src.clients.binding_verification import classify_binding_verification
    from src.clients import readiness_store
    from src.core.database import get_db_context, init_db
    from src.core.models import Account
    from src.core.scheduler_models import AccountTargetBinding, ChatTarget
    from src.dashboard.operator_control_service import queue_counts_snapshot
    from src.telegram_gateway.models import TelegramGatewayJob
    from sqlalchemy import func

    init_db()
    blockers: list[str] = []
    warnings: list[str] = []

    flags = {
        "AUTOSTORY_PRODUCTION_CERTIFIED_NO_GO": _env_flag("AUTOSTORY_PRODUCTION_CERTIFIED_NO_GO"),
        "PROMO_GENERATION_MODE": _env_flag("PROMO_GENERATION_MODE"),
        "SCHEDULER_MUTATIONS_ENABLED": _env_flag("SCHEDULER_MUTATIONS_ENABLED"),
        "P5C_SINGLE_SEND_ENABLED": _env_flag("P5C_SINGLE_SEND_ENABLED"),
        "P5D_SINGLE_SEND_ENABLED": _env_flag("P5D_SINGLE_SEND_ENABLED"),
        "P6_4_SINGLE_SEND_ENABLED": _env_flag("P6_4_SINGLE_SEND_ENABLED"),
    }
    if flags["AUTOSTORY_PRODUCTION_CERTIFIED_NO_GO"].lower() not in ("true", "1", "yes", "on"):
        blockers.append("NO_GO not true")
    if flags["SCHEDULER_MUTATIONS_ENABLED"].lower() in ("true", "1", "yes", "on"):
        blockers.append("scheduler mutations enabled")
    if flags["PROMO_GENERATION_MODE"].lower() not in ("", "disabled", "off", "false"):
        blockers.append("promo generation not disabled")
    for k in ("P5C_SINGLE_SEND_ENABLED", "P5D_SINGLE_SEND_ENABLED", "P6_4_SINGLE_SEND_ENABLED"):
        if flags[k].lower() in ("true", "1", "yes", "on") and not execution_mode:
            warnings.append(f"{k} unexpectedly true during dry-run")
        if flags[k].lower() in ("true", "1", "yes", "on") and execution_mode and k != "P6_4_SINGLE_SEND_ENABLED":
            blockers.append(f"{k} must remain false for execution preflight")

    services = {
        "autostory-web": _svc("autostory-web"),
        "autostory-scheduler": _svc("autostory-scheduler"),
        "autostory-readiness-worker": _svc("autostory-readiness-worker"),
        "telegram-gateway": _svc("telegram-gateway"),
        "kathleen-account-listener": _svc("kathleen-account-listener"),
        "storyfleet-bot": _svc("storyfleet-bot"),
    }
    if services["telegram-gateway"]["active"] == "active":
        blockers.append("telegram-gateway active")
    if services["telegram-gateway"]["enabled"] == "enabled":
        blockers.append("telegram-gateway enabled")
    if services["autostory-readiness-worker"]["active"] != "active":
        blockers.append("readiness worker inactive")

    p6_2 = _p6_2_result()
    if execution_mode and not p6_2.get("ok"):
        blockers.append("P6.2 certification not PASS")
    elif not p6_2.get("ok"):
        warnings.append("P6.2 not PASS yet (expected before 24h threshold)")

    with get_db_context() as db:
        queue = queue_counts_snapshot(db)
        pending_gw = (
            db.query(func.count(TelegramGatewayJob.id))
            .filter(TelegramGatewayJob.status.in_(("pending", "retry", "running")))
            .scalar()
            or 0
        )
        if int(queue.get("pending_jobs") or 0) or int(queue.get("running_jobs") or 0):
            blockers.append("executable scheduled jobs pending/running")
        if int(pending_gw) > 0:
            blockers.append(f"gateway backlog pending/retry/running={pending_gw}")

        account = db.query(Account).filter(Account.id == int(account_id)).first()
        target = db.query(ChatTarget).filter(ChatTarget.id == int(target_id)).first()
        binding = db.query(AccountTargetBinding).filter(AccountTargetBinding.id == int(binding_id)).first()
        ver = classify_binding_verification(db, int(account_id), int(target_id))
        snap = readiness_store.fetch_snapshot(db, int(account_id))
        ready_ok = bool(
            snap
            and snap.status == readiness_store.STAT_READY
            and readiness_store.snapshot_ready_and_valid(db, int(account_id))
        )
        if account is None:
            blockers.append("account missing")
        if target is None:
            blockers.append("target missing")
        if binding is None:
            blockers.append("binding missing")
        elif int(binding.account_id) != int(account_id) or int(binding.target_id) != int(target_id):
            blockers.append("binding does not match account/target")
        if not ready_ok:
            blockers.append("account readiness not READY+fresh")
        if not ver.get("production_verified"):
            blockers.append(f"binding not production-verified: {ver.get('status')}")

        import sqlite3

        conn = sqlite3.connect(str(_REPO / "data/storyfleet.db"))
        try:
            quick = conn.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            conn.close()
        if quick != "ok":
            blockers.append(f"quick_check={quick}")

    content_sha = _content_hash(content) if content else None
    if not content:
        warnings.append("no content provided — hash not computed")

    hard_blockers = [b for b in blockers if "P6.2" not in b]
    if execution_mode:
        verdict = (
            "P6_4_EXECUTION_PREFLIGHT_PASS"
            if not blockers
            else "P6_4_EXECUTION_PREFLIGHT_BLOCKED"
        )
    elif hard_blockers:
        verdict = "P6_4_DRY_RUN_BLOCKED"
    elif not p6_2.get("ok"):
        verdict = "P6_4_DRY_RUN_READY_EXECUTION_BLOCKED_BY_P6_2_TIME_GATE"
    else:
        verdict = "P6_4_DRY_RUN_READY"

    return {
        "checked_at_utc": _iso_now(),
        "mode": "execution-preflight" if execution_mode else "dry-run",
        "verdict": verdict,
        "blockers": blockers,
        "warnings": warnings,
        "account_id": account_id,
        "target_id": target_id,
        "binding_id": binding_id,
        "account_ready": ready_ok,
        "binding_verification": ver,
        "services": services,
        "flags": flags,
        "queue": queue,
        "pending_gateway": int(pending_gw),
        "p6_2": {
            "ok": p6_2.get("ok"),
            "final_verdict": p6_2.get("final_p6_2_verdict"),
            "elapsed_seconds": p6_2.get("elapsed_seconds"),
            "checkpoint_count": p6_2.get("checkpoint_count"),
            "blockers": p6_2.get("blockers"),
        },
        "content_sha256": content_sha,
        "database_quick_check": quick,
        "mutated": False,
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--account-id", type=int, default=DEFAULT_ACCOUNT)
    p.add_argument("--target-id", type=int, default=DEFAULT_TARGET)
    p.add_argument("--binding-id", type=int, default=DEFAULT_BINDING)
    p.add_argument("--content-file", default="")
    p.add_argument("--content", default="")
    p.add_argument("--dry-run", action="store_true", default=True)
    p.add_argument("--execution-preflight", action="store_true")
    p.add_argument("--out", default="")
    args = p.parse_args()
    content = args.content
    if args.content_file:
        content = Path(args.content_file).read_text(encoding="utf-8")
    if not content:
        content = (
            "STORYFLEET canary check — harmless test post. No action required."
        )
    result = run_preflight(
        account_id=args.account_id,
        target_id=args.target_id,
        binding_id=args.binding_id,
        content=content,
        execution_mode=bool(args.execution_preflight),
    )
    out = args.out or str(
        _REPO
        / "data/audit"
        / f"p6_4_preflight_{'exec' if args.execution_preflight else 'dry'}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    )
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, default=str))
    if result["verdict"].endswith("_BLOCKED") and "TIME_GATE" not in result["verdict"]:
        return 2
    if "TIME_GATE" in result["verdict"]:
        return 0
    if result["verdict"].endswith("_PASS") or result["verdict"].endswith("_READY"):
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
