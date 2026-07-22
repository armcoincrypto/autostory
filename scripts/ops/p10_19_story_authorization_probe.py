#!/usr/bin/env python3
"""P10.19 fresh Story authorization probe (ops tooling).

Never publishes a story. Supports --inspect / --dry-run / --apply.
Dry-run may call Telegram CanSendStoryRequest via product helpers; --apply
only persists story-precheck cache fields.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.database import get_db_context
from src.core.models import Account
from src.core.scheduler_models import AccountReadinessSnapshot
from src.core.session_paths import account_has_canonical_session, get_session_readiness
from src.stories.precheck import persist_precheck_result, run_story_precheck
from src.stories.rotation_audit import (
    CONTROLLED_LIVE_ACCOUNT_ID,
    build_story_rotation_precheck,
    story_auth_is_fresh,
)

REPORTS_DIR = Path("data/recovery_lab/reports")


def _probe_allow_ids() -> frozenset[int]:
    raw = os.environ.get("STORY_AUTH_PROBE_ALLOW_IDS", "").strip()
    if not raw:
        return frozenset({CONTROLLED_LIVE_ACCOUNT_ID})
    return frozenset(int(x.strip()) for x in raw.split(",") if x.strip())


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        return iso()
    text = str(value).strip()
    return text or None


def _account_snapshot(account_id: int) -> dict[str, Any]:
    with get_db_context() as db:
        account = db.get(Account, int(account_id))
        snap = (
            db.query(AccountReadinessSnapshot)
            .filter(AccountReadinessSnapshot.account_id == int(account_id))
            .first()
        )
        if not account:
            return {"account_id": int(account_id), "found": False}
        precheck = build_story_rotation_precheck(
            db,
            {
                "account_ids": [int(account_id)],
                "mentions_per_story": 0,
                "dry_run": True,
            },
        )
        return {
            "account_id": int(account_id),
            "found": True,
            "status": getattr(account.status, "value", account.status),
            "purpose": account.purpose,
            "health_status": account.health_status,
            "health_reason": account.health_reason,
            "health_checked_at": _iso(account.health_checked_at),
            "has_canonical_session": account_has_canonical_session(account),
            "session_readiness": get_session_readiness(account),
            "readiness_snapshot": {
                "status": snap.status if snap else None,
                "reason": snap.reason if snap else None,
                "checked_at": _iso(snap.checked_at) if snap else None,
                "expires_at": _iso(snap.expires_at) if snap else None,
            },
            "story_precheck_status": account.story_precheck_status,
            "story_precheck_reason": getattr(account, "story_precheck_reason", None),
            "story_precheck_checked_at": _iso(account.story_precheck_checked_at),
            "fresh_story_auth_ok": story_auth_is_fresh(account),
            "story_status": getattr(account, "story_status", None),
            "story_status_reason": getattr(account, "story_status_reason", None),
            "story_blocked_until": _iso(getattr(account, "story_blocked_until", None)),
            "last_story_success_at": _iso(getattr(account, "last_story_success_at", None)),
            "last_story_attempt_at": _iso(getattr(account, "last_story_attempt_at", None)),
            "stories_today": int(account.stories_today or 0),
            "precheck_projection": precheck,
        }


async def _run_probe(account_id: int) -> dict[str, Any]:
    from src.clients.manager import client_manager

    wrapper, err = await client_manager.get_fresh_client_for_story_publish(account_id)
    if err or not wrapper:
        return {
            "account_id": int(account_id),
            "status": "failed_check",
            "reason": err or "no_client",
            "checked_at": None,
            "retry_after_seconds": None,
        }
    try:
        return await run_story_precheck(wrapper.client, account_id)
    finally:
        if getattr(wrapper, "_precheck_disconnect_after", False):
            try:
                await wrapper.disconnect()
            except Exception:
                pass


def _write_reports(report: dict[str, Any]) -> tuple[Path, Path]:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    json_path = REPORTS_DIR / f"p10_19_story_authorization_probe_{ts}.json"
    md_path = REPORTS_DIR / f"p10_19_story_authorization_probe_{ts}.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8")
    md_path.write_text(
        "\n".join(
            [
                "# P10.19 Story Authorization Probe",
                "",
                f"- mode: `{report['mode']}`",
                f"- account: `{report['account_id']}`",
                f"- no_publish: `{report['safety']['no_publish']}`",
                f"- fresh_story_auth_ok_before: `{report['before'].get('fresh_story_auth_ok')}`",
                f"- probe_status: `{(report.get('probe') or {}).get('status')}`",
                f"- applied: `{report.get('applied')}`",
                f"- fresh_story_auth_ok_after: `{(report.get('after') or {}).get('fresh_story_auth_ok')}`",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return json_path, md_path


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--inspect", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--account", type=int, default=CONTROLLED_LIVE_ACCOUNT_ID)
    args = parser.parse_args()

    allow_ids = _probe_allow_ids()
    if int(args.account) not in allow_ids:
        raise SystemExit(
            f"P10.19 account {args.account} not in allowlist {sorted(allow_ids)}. "
            "Set STORY_AUTH_PROBE_ALLOW_IDS for controlled cohort probes."
        )

    selected_mode = "inspect" if args.inspect else ("dry-run" if args.dry_run else "apply")
    before = _account_snapshot(args.account)
    probe = None
    applied = False
    if args.dry_run or args.apply:
        probe = asyncio.run(_run_probe(args.account))
        if args.apply and probe.get("status"):
            persist_precheck_result(
                args.account,
                probe["status"],
                probe.get("reason", ""),
                probe.get("retry_after_seconds"),
            )
            applied = True
    after = _account_snapshot(args.account)
    report = {
        "phase": "P10.19",
        "generated_at": datetime.utcnow().isoformat(),
        "mode": selected_mode,
        "account_id": args.account,
        "before": before,
        "probe": probe,
        "applied": applied,
        "after": after,
        "safety": {
            "no_publish": True,
            "no_send": True,
            "no_join": True,
            "account_scope": [args.account],
            "updates_only_story_precheck_cache": bool(args.apply),
        },
    }
    json_path, md_path = _write_reports(report)
    print("p10_19_story_authorization_probe")
    print(json_path)
    print(md_path)
    if probe and probe.get("status") != "allowed":
        return 2 if args.apply else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
