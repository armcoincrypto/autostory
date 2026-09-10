#!/usr/bin/env python3
"""Storyfleet ops health check (Wave L) — read-only, no Telegram/OpenAI probes.

Writes:
  /opt/autostory/data/runtime/ops_health_latest.json
  /opt/autostory/data/runtime/ops_health_alert_state.json

Emits journal lines for new/recovered material issues (deduplicated).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

# Allow running from immutable release via PYTHONPATH=/opt/autostory-releases/current
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Storyfleet consolidated ops health")
    parser.add_argument("--json", action="store_true", help="Print full JSON report")
    parser.add_argument("--quiet", action="store_true", help="No stdout summary")
    parser.add_argument("--no-write", action="store_true", help="Do not persist state files")
    parser.add_argument("--force-quick-check", action="store_true")
    args = parser.parse_args(argv)

    # Load production .env lightly for flags (does not override existing env)
    env_path = Path("/opt/autostory/.env")
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

    from src.ops.ops_health import (
        compute_alert_events,
        build_ops_health_report,
        load_alert_state,
        load_previous_state,
        write_alert_state,
        write_ops_health_report,
    )

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    log = logging.getLogger("storyfleet-ops-health")

    previous = load_previous_state()
    report = build_ops_health_report(
        previous_state=previous,
        run_quick_check=True if args.force_quick_check else None,
    )
    alert_prev = load_alert_state()
    events, alert_new = compute_alert_events(report, previous_alert_state=alert_prev)

    if not args.no_write:
        write_ops_health_report(report)
        write_alert_state(alert_new)

    for ev in events:
        # journal-friendly single line; no secrets / message bodies
        msg = (
            f"storyfleet_ops_alert type={ev.get('type')} name={ev.get('name')} "
            f"state={ev.get('state')} summary={ev.get('summary')}"
        )
        if ev.get("state") == "critical" or ev.get("type") == "new" and ev.get("state") == "critical":
            log.error(msg)
        else:
            log.warning(msg)

    if args.json:
        print(json.dumps(report, indent=2))
    elif not args.quiet:
        print(f"status={report.get('status')} owner_copy={report.get('owner_copy')}")
        print(f"alerts_emitted={len(events)} open_issues={len(alert_new.get('open') or {})}")

    if report.get("status") == "critical":
        return 2
    if report.get("status") == "warning":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
