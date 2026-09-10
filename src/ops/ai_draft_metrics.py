"""Wave L — AI Draft usage metrics (no prompts, no message bodies)."""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_LOCK = threading.Lock()

DEFAULT_METRICS_PATH = Path(
    os.environ.get("STORYFLEET_AI_DRAFT_METRICS_PATH")
    or "/opt/autostory/data/runtime/ai_draft_metrics.jsonl"
)
# Bound file growth (Wave G philosophy)
MAX_METRICS_LINES = int(os.environ.get("STORYFLEET_AI_DRAFT_METRICS_MAX_LINES") or "5000")
FAILURE_SPIKE_WINDOW_SEC = int(os.environ.get("STORYFLEET_AI_DRAFT_FAIL_WINDOW_SEC") or "3600")
FAILURE_SPIKE_THRESHOLD = int(os.environ.get("STORYFLEET_AI_DRAFT_FAIL_THRESHOLD") or "3")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def record_ai_draft_metric(
    *,
    ok: bool,
    error_code: Optional[str] = None,
    model: str = "",
    latency_ms: Optional[int] = None,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    path: Path | None = None,
) -> None:
    """Append one privacy-safe metric row. Never stores prompts or drafts."""
    target = path or DEFAULT_METRICS_PATH
    row = {
        "ts": _utcnow_iso(),
        "ok": bool(ok),
        "error_code": (error_code or None),
        "model": (model or "")[:64] or None,
        "latency_ms": int(latency_ms) if latency_ms is not None else None,
        "input_tokens": int(input_tokens) if input_tokens is not None else None,
        "output_tokens": int(output_tokens) if output_tokens is not None else None,
    }
    line = json.dumps(row, separators=(",", ":")) + "\n"
    with _LOCK:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as fh:
                fh.write(line)
            _trim_file(target)
        except OSError:
            # Observability must never break drafting.
            return


def _trim_file(path: Path) -> None:
    try:
        raw = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(raw) <= MAX_METRICS_LINES:
        return
    keep = raw[-MAX_METRICS_LINES :]
    path.write_text("\n".join(keep) + "\n", encoding="utf-8")


def _parse_ts(value: str) -> Optional[float]:
    text = (value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def load_ai_draft_metrics(*, path: Path | None = None, max_lines: int = 2000) -> list[dict[str, Any]]:
    target = path or DEFAULT_METRICS_PATH
    if not target.is_file():
        return []
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines[-max_lines:]:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def summarize_ai_draft_metrics(
    rows: list[dict[str, Any]] | None = None,
    *,
    now: Optional[float] = None,
    day_utc: Optional[str] = None,
) -> dict[str, Any]:
    now = now if now is not None else time.time()
    rows = rows if rows is not None else load_ai_draft_metrics()
    if day_utc is None:
        day_utc = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")

    today_ok = today_fail = 0
    in_tok = out_tok = 0
    latencies: list[int] = []
    recent_failures: list[str] = []
    window_fail = 0
    consecutive_fail = 0
    streak = 0

    for row in rows:
        ts = _parse_ts(str(row.get("ts") or ""))
        ok = bool(row.get("ok"))
        day = None
        if ts is not None:
            day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        if day == day_utc:
            if ok:
                today_ok += 1
            else:
                today_fail += 1
            if row.get("input_tokens") is not None:
                in_tok += int(row["input_tokens"] or 0)
            if row.get("output_tokens") is not None:
                out_tok += int(row["output_tokens"] or 0)
            if row.get("latency_ms") is not None:
                try:
                    latencies.append(int(row["latency_ms"]))
                except (TypeError, ValueError):
                    pass
        if ts is not None and now - ts <= FAILURE_SPIKE_WINDOW_SEC and not ok:
            window_fail += 1
            code = str(row.get("error_code") or "error")
            recent_failures.append(code)

    # Consecutive failures from the end of the log
    for row in reversed(rows):
        if bool(row.get("ok")):
            break
        streak += 1
    consecutive_fail = streak

    avg_latency = int(sum(latencies) / len(latencies)) if latencies else None
    spike = consecutive_fail >= FAILURE_SPIKE_THRESHOLD or window_fail >= FAILURE_SPIKE_THRESHOLD

    return {
        "day_utc": day_utc,
        "requests_today": today_ok + today_fail,
        "success_today": today_ok,
        "failed_today": today_fail,
        "input_tokens_today": in_tok,
        "output_tokens_today": out_tok,
        "avg_latency_ms_today": avg_latency,
        "recent_failure_codes": recent_failures[-10:],
        "consecutive_failures": consecutive_fail,
        "failures_in_window": window_fail,
        "failure_spike": spike,
        "prompts_persisted": False,
    }
