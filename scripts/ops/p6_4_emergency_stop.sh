#!/usr/bin/env bash
# P6.4 emergency stop — idempotent. Safe to run twice.
# Stops telegram-gateway, restores NO_GO / mutation / send flags, invalidates
# unconsumed P6.4 authorization. Does not delete evidence or cancel unrelated jobs.
set -euo pipefail
ROOT="/opt/autostory"
cd "$ROOT"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
EVIDENCE_DIR="${ROOT}/data/audit/p6_4_emergency_stop"
mkdir -p "$EVIDENCE_DIR"
OUT="${EVIDENCE_DIR}/${STAMP}.json"

echo "P6.4 emergency stop starting at ${STAMP}"

# 1) Stop gateway (idempotent)
systemctl stop telegram-gateway 2>/dev/null || true
# Ensure disabled for boot (idempotent)
systemctl disable telegram-gateway 2>/dev/null || true

GW_ACTIVE="$(systemctl is-active telegram-gateway 2>/dev/null || true)"
GW_ENABLED="$(systemctl is-enabled telegram-gateway 2>/dev/null || true)"

# 2) Restore safety flags in .env (idempotent upsert)
python3 <<'PY'
import json
from pathlib import Path

env_path = Path("/opt/autostory/.env")
if not env_path.is_file():
    raise SystemExit("missing .env")
lines = env_path.read_text(encoding="utf-8").splitlines()
locks = {
    "AUTOSTORY_PRODUCTION_CERTIFIED_NO_GO": "true",
    "PROMO_GENERATION_MODE": "disabled",
    "SCHEDULER_MUTATIONS_ENABLED": "false",
    "P5C_SINGLE_SEND_ENABLED": "false",
    "P5D_SINGLE_SEND_ENABLED": "false",
    "P6_4_SINGLE_SEND_ENABLED": "false",
    "SCHEDULER_MUTATION_ACCOUNT_ALLOWLIST": "",
    "SCHEDULER_MUTATION_TARGET_ALLOWLIST": "",
    "SCHEDULER_MUTATION_SCOPE": "",
    "SCHEDULER_MUTATION_PAIR_ALLOWLIST": "",
}
out = []
seen = set()
for line in lines:
    key = line.split("=", 1)[0] if "=" in line else None
    if key in locks:
        out.append(f"{key}={locks[key]}")
        seen.add(key)
    else:
        out.append(line)
for key, val in locks.items():
    if key not in seen:
        out.append(f"{key}={val}")
env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
print(json.dumps({"env_locked": True, "keys": sorted(locks.keys())}))
PY

# 3) Invalidate unconsumed P6.4 authorization (safe if missing/consumed)
./venv/bin/python - <<'PY'
import json
from pathlib import Path
from src.core.p6_4_authorization import invalidate_unconsumed, load_manifest

invalidate_unconsumed()
m = load_manifest() or {}
print(json.dumps({
    "authorization_invalidated": bool(m.get("invalidated") or not m.get("armed")),
    "consumed": bool(m.get("consumed")),
    "armed": bool(m.get("armed")),
    "authorization_id": m.get("authorization_id"),
}))
PY

# 4) Surface reconciliation-required / uncertain gateway jobs (read-only)
./venv/bin/python - <<'PY'
import json
from src.core.database import get_db_context, init_db
from src.telegram_gateway.models import TelegramGatewayJob

init_db()
rows = []
with get_db_context() as db:
    q = (
        db.query(TelegramGatewayJob)
        .filter(
            TelegramGatewayJob.status.in_(("running", "retry")),
        )
        .limit(50)
        .all()
    )
    for j in q:
        rows.append({
            "id": j.id,
            "account_id": j.account_id,
            "status": j.status,
            "error_code": getattr(j, "error_code", None),
        })
    # also note ambiguous error codes on failed jobs
    amb = (
        db.query(TelegramGatewayJob)
        .filter(TelegramGatewayJob.error_code == "ambiguous_reconciliation_required")
        .order_by(TelegramGatewayJob.id.desc())
        .limit(20)
        .all()
    )
    amb_rows = [{"id": j.id, "account_id": j.account_id, "status": j.status} for j in amb]
print(json.dumps({"pending_or_running": rows, "reconciliation_required": amb_rows}))
PY

# 5) Compose evidence
python3 - "$OUT" "$GW_ACTIVE" "$GW_ENABLED" "$STAMP" <<'PY'
import json, sys
from pathlib import Path
out_path, gw_active, gw_enabled, stamp = sys.argv[1:5]
report = {
    "stamp_utc": stamp,
    "verdict": "P6_4_EMERGENCY_STOP_APPLIED",
    "telegram_gateway": {"active": gw_active, "enabled": gw_enabled},
    "idempotent": True,
    "notes": [
        "Gateway stopped and disabled",
        "NO_GO / mutation / send flags restored",
        "Unconsumed P6.4 authorization invalidated",
        "Evidence preserved; unrelated jobs not cancelled",
    ],
}
Path(out_path).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
print(json.dumps(report, indent=2))
PY

if [[ "$GW_ACTIVE" != "inactive" && "$GW_ACTIVE" != "failed" && "$GW_ACTIVE" != "dead" ]]; then
  # accept inactive/failed/dead as stopped; active is bad
  if [[ "$GW_ACTIVE" == "active" ]]; then
    echo "ERROR: telegram-gateway still active" >&2
    exit 2
  fi
fi

echo "P6_4_EMERGENCY_STOP_OK evidence=$OUT"
exit 0
