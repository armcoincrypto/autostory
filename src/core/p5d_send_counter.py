"""P5D independent send counter for gateway restart durability certification."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

STATE_PATH = Path("data/audit/p5d_single_send_state.json")
PHASE = "P5D"
SCOPE = "gateway_restart_durability"


def _max_allowed() -> int:
    raw = (os.environ.get("P5D_SINGLE_SEND_MAX") or "3").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 3


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


def reset_p5d_send_counter() -> None:
    if STATE_PATH.is_file():
        STATE_PATH.unlink(missing_ok=True)


def p5d_live_send_count() -> int:
    return int(_load().get("live_sends") or 0)


def p5d_live_send_remaining() -> int:
    return max(0, _max_allowed() - p5d_live_send_count())


def record_p5d_live_send(
    *,
    scenario: str,
    job_id: int,
    account_id: int,
    target_id: int,
    authorization_id: str,
    gateway_job_id: Optional[int] = None,
    delivery_id: Optional[int] = None,
    tg_message_id: Optional[int] = None,
    reconciled: bool = False,
) -> dict[str, Any]:
    state = _load()
    state["live_sends"] = int(state.get("live_sends") or 0) + 1
    events = list(state.get("events") or [])
    events.append(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "scenario": scenario,
            "job_id": int(job_id),
            "account_id": int(account_id),
            "target_id": int(target_id),
            "authorization_id": authorization_id,
            "gateway_job_id": gateway_job_id,
            "delivery_id": delivery_id,
            "tg_message_id": tg_message_id,
            "reconciled": bool(reconciled),
        }
    )
    state["events"] = events[-30:]
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save(state)
    return state


def p5d_counter_snapshot() -> dict[str, Any]:
    used = p5d_live_send_count()
    limit = _max_allowed()
    return {"phase": PHASE, "scope": SCOPE, "used": used, "limit": limit, "remaining": max(0, limit - used)}
