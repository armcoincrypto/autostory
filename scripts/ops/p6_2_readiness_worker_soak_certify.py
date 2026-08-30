#!/usr/bin/env python3
"""P6.2A final 24h soak certification evaluator (read-only)."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_REPO = Path(__file__).resolve().parents[2]
MIN_ELAPSED_SEC = 86400
MIN_CHECKPOINTS = 48
MAX_GAP_SEC = 3600


def _parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(s.strip().replace("Z", "+00:00")).astimezone(timezone.utc)


def _load_checkpoints(directory: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for f in sorted(directory.glob("checkpoint_*.json")):
        if f.name == "checkpoint_latest.json":
            continue
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return out


def evaluate(*, baseline: dict[str, Any], checkpoints: list[dict[str, Any]], end_utc: datetime) -> dict[str, Any]:
    start = _parse_utc(str(baseline["soak_start_utc"]))
    elapsed = (end_utc - start).total_seconds()
    blockers: list[str] = []
    warnings: list[str] = []
    if elapsed < MIN_ELAPSED_SEC:
        blockers.append(f"elapsed {elapsed:.0f}s < {MIN_ELAPSED_SEC}s required")
    if len(checkpoints) < MIN_CHECKPOINTS:
        blockers.append(f"checkpoint count {len(checkpoints)} < {MIN_CHECKPOINTS}")
    gaps: list[float] = []
    prev: Optional[datetime] = None
    for cp in checkpoints:
        ts = _parse_utc(str(cp["checkpoint_utc"]))
        if prev is not None:
            gaps.append((ts - prev).total_seconds())
        prev = ts
    max_gap = max(gaps) if gaps else 0.0
    if max_gap > MAX_GAP_SEC:
        blockers.append(f"maximum checkpoint gap {max_gap:.0f}s > {MAX_GAP_SEC}s")
    worker_active = sum(
        1 for cp in checkpoints
        if (cp.get("services") or {}).get("autostory-readiness-worker", {}).get("active") == "active"
    )
    worker_avail = worker_active / len(checkpoints) if checkpoints else 0.0
    if checkpoints and worker_avail < 0.99:
        blockers.append(f"worker availability {worker_avail:.2%} < 99%")
    cycle_durations = [
        float((cp.get("cycle_metrics") or {}).get("last_cycle_duration_sec") or 0)
        for cp in checkpoints
        if (cp.get("cycle_metrics") or {}).get("last_cycle_duration_sec")
    ]
    max_cycle = max(cycle_durations) if cycle_durations else 0.0
    last = checkpoints[-1] if checkpoints else {}
    last_ev = last.get("evaluation") or {}
    blockers.extend(f"latest checkpoint: {b}" for b in last_ev.get("blockers", []))
    if (last.get("account_107") or baseline.get("account_107") or {}).get("status") != "READY":
        blockers.append("account 107 not READY at end")
    if (last.get("account_139") or baseline.get("account_139") or {}).get("status") != "NOT_AUTHORIZED":
        blockers.append("account 139 not NOT_AUTHORIZED at end")
    warnings.extend(last_ev.get("warnings", []))
    verdict = "BLOCKED" if blockers else ("PASS_WITH_WARNING" if warnings else "PASS")
    return {
        "verdict": verdict,
        "exit_code": 2 if blockers else (1 if warnings else 0),
        "soak_id": baseline.get("soak_id"),
        "soak_start_utc": baseline.get("soak_start_utc"),
        "soak_end_utc": end_utc.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "elapsed_seconds": elapsed,
        "elapsed_hours": round(elapsed / 3600.0, 3),
        "checkpoint_count": len(checkpoints),
        "maximum_checkpoint_gap_sec": max_gap,
        "worker_availability_ratio": worker_avail,
        "maximum_cycle_duration_sec": max_cycle,
        "blockers": blockers,
        "warnings": warnings,
        "final_p6_2_verdict": (
            "P6_2_BACKGROUND_READINESS_WORKER_ACTIVATION_AND_SOAK_CERTIFICATION_PASS" if verdict == "PASS"
            else "P6_2_EXTENDED_SOAK_PASS_WITH_WARNINGS" if verdict == "PASS_WITH_WARNING"
            else "P6_2_EXTENDED_SOAK_BLOCKED"
        ),
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--baseline", required=True)
    p.add_argument("--checkpoints-dir", default=str(_REPO / "data/audit/p6_2_soak_checkpoints"))
    p.add_argument("--end-utc", default="")
    a = p.parse_args()
    baseline = json.loads(Path(a.baseline).read_text(encoding="utf-8"))
    result = evaluate(
        baseline=baseline,
        checkpoints=_load_checkpoints(Path(a.checkpoints_dir)),
        end_utc=_parse_utc(a.end_utc) if a.end_utc else datetime.now(timezone.utc),
    )
    now = datetime.now(timezone.utc)
    out = _REPO / "data/audit" / f"p6_2_soak_cert_{now.strftime('%Y%m%dT%H%M%SZ')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    md = _REPO / "docs/audits/P6_2_BACKGROUND_READINESS_WORKER_SOAK_CERTIFICATION.md"
    blockers_lines = [f"- {b}" for b in result["blockers"]] or ["- none"]
    warnings_lines = [f"- {w}" for w in result["warnings"]] or ["- none"]
    md.write_text(
        "\n".join([
            "# P6.2 Background Readiness Worker Soak Certification",
            "",
            f"**Verdict:** `{result['final_p6_2_verdict']}`",
            "",
            f"- soak_start_utc: `{result['soak_start_utc']}`",
            f"- soak_end_utc: `{result['soak_end_utc']}`",
            f"- elapsed_seconds: `{result['elapsed_seconds']:.0f}`",
            f"- checkpoint_count: `{result['checkpoint_count']}`",
            "",
            "## Blockers",
            *blockers_lines,
            "",
            "## Warnings",
            *warnings_lines,
        ]),
        encoding="utf-8",
    )
    print(json.dumps(result, indent=2))
    return int(result["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
