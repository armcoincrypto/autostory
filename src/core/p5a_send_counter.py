"""P5A independent single-send counter — separate from P4C."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

STATE_PATH = Path("data/audit/p5a_single_send_state.json")
PHASE = "P5A"
SCOPE = "scoped_single_account_repeatability"


def _max_allowed() -> int:
    raw = (os.environ.get("P5A_SINGLE_SEND_MAX") or "1").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 1


def _load() -> dict[str, Any]:
    if not STATE_PATH.is_file():
        return {"phase": PHASE, "scope": SCOPE, "live_sends": 0, "events": []}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"phase": PHASE, "scope": SCOPE, "live_sends": 0, "events": []}


def _save(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state["phase"] = PHASE
    state["scope"] = SCOPE
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def p5a_live_send_count() -> int:
    return int(_load().get("live_sends") or 0)


def p5a_live_send_remaining() -> int:
    return max(0, _max_allowed() - p5a_live_send_count())


def record_p5a_live_send(
    *,
    job_id: int,
    account_id: int,
    target_id: int,
    authorization_id: str,
    tg_message_id: Optional[int] = None,
) -> dict[str, Any]:
    state = _load()
    state["live_sends"] = int(state.get("live_sends") or 0) + 1
    events = list(state.get("events") or [])
    events.append(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "job_id": int(job_id),
            "account_id": int(account_id),
            "target_id": int(target_id),
            "authorization_id": authorization_id,
            "tg_message_id": tg_message_id,
        }
    )
    state["events"] = events[-20:]
    state["authorization_id"] = authorization_id
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save(state)
    return state


def p5a_counter_snapshot() -> dict[str, Any]:
    used = p5a_live_send_count()
    limit = _max_allowed()
    return {
        "phase": PHASE,
        "scope": SCOPE,
        "used": used,
        "limit": limit,
        "remaining": max(0, limit - used),
    }
