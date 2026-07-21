"""P4C single-send counter — persisted audit state for scoped controlled send."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_PATH = Path("data/audit/p4c_single_send_state.json")


def _max_allowed() -> int:
    raw = (os.environ.get("P4C_SINGLE_SEND_MAX") or "1").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 1


def _load() -> dict[str, Any]:
    if not STATE_PATH.is_file():
        return {"live_sends": 0, "events": []}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"live_sends": 0, "events": []}


def _save(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def p4c_live_send_count() -> int:
    return int(_load().get("live_sends") or 0)


def p4c_live_send_remaining() -> int:
    return max(0, _max_allowed() - p4c_live_send_count())


def record_p4c_live_send(*, job_id: int, account_id: int, target_id: int) -> dict[str, Any]:
    state = _load()
    state["live_sends"] = int(state.get("live_sends") or 0) + 1
    events = list(state.get("events") or [])
    events.append(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "job_id": int(job_id),
            "account_id": int(account_id),
            "target_id": int(target_id),
        }
    )
    state["events"] = events[-20:]
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save(state)
    return state


def reset_p4c_send_counter() -> None:
    if STATE_PATH.is_file():
        STATE_PATH.unlink(missing_ok=True)
