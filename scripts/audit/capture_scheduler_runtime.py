#!/usr/bin/env python3
"""Capture scheduler/worker runtime evidence without triggering work."""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
from pathlib import Path


SAFE_CONFIG_KEYS = {
    "SCHEDULER_MUTATIONS_ENABLED",
    "SCHEDULER_MUTATION_SCOPE",
    "CAMPAIGN_EXECUTION_ENABLED",
    "STORY_EXECUTION_ENABLED",
    "DISCOVERY_EXECUTION_ENABLED",
    "READINESS_WORKER_ENABLED",
    "AI_AGENT_AUTO_LOOP_ENABLED",
    "PROMO_GENERATION_MODE",
    "AUTOSTORY_PRODUCTION_CERTIFIED_NO_GO",
    "KATHLEEN_ACCOUNT_LISTENER_ENABLED",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--service", action="append", default=[])
    parser.add_argument("--database", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def service_state(name: str) -> dict:
    properties = (
        "Id,LoadState,ActiveState,SubState,ExecMainPID,ExecMainStartTimestamp,"
        "NRestarts,FragmentPath,User,ExecStart"
    )
    result = subprocess.run(
        ["systemctl", "show", name, f"--property={properties}", "--no-pager"],
        check=False,
        capture_output=True,
        text=True,
    )
    values = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    values["inspection_returncode"] = result.returncode
    return values


def safe_config(path: Path | None) -> dict[str, str]:
    if path is None or not path.is_file():
        return {}
    result: dict[str, str] = {}
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            stripped = raw.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key = key.strip()
            if key in SAFE_CONFIG_KEYS:
                result[key] = value.strip()
    return result


def database_evidence(path: Path | None) -> dict:
    if path is None:
        return {}
    if not path.is_file():
        raise SystemExit(f"database not found: {path}")
    uri = f"file:{path.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        tables = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        result: dict[str, object] = {}
        if "scheduled_jobs" in tables:
            result["scheduled_jobs_by_status"] = {
                str(status): int(count)
                for status, count in db.execute(
                    "SELECT COALESCE(status, '<null>'), count(*) "
                    "FROM scheduled_jobs GROUP BY status ORDER BY status"
                )
            }
            result["scheduled_jobs_latest_updated_at"] = db.execute(
                "SELECT max(updated_at) FROM scheduled_jobs"
            ).fetchone()[0]
        if "telegram_gateway_jobs" in tables:
            result["telegram_gateway_jobs_by_status"] = {
                str(status): int(count)
                for status, count in db.execute(
                    "SELECT COALESCE(status, '<null>'), count(*) "
                    "FROM telegram_gateway_jobs GROUP BY status ORDER BY status"
                )
            }
            columns = {
                row[1] for row in db.execute("PRAGMA table_info(telegram_gateway_jobs)")
            }
            timestamp = "updated_at" if "updated_at" in columns else "created_at"
            result["telegram_gateway_latest_timestamp"] = db.execute(
                f"SELECT max({timestamp}) FROM telegram_gateway_jobs"
            ).fetchone()[0]
        return result


def main() -> int:
    args = parse_args()
    payload = {
        "inspection_only": True,
        "jobs_triggered": 0,
        "services": {
            name: service_state(name)
            for name in sorted(set(args.service))
        },
        "safe_configuration": safe_config(args.env_file),
        "database_evidence": database_evidence(args.database),
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            f"services={len(payload['services'])} jobs_triggered=0 "
            f"database_evidence={bool(payload['database_evidence'])}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
