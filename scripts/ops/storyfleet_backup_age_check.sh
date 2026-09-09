#!/usr/bin/env bash
# Fail if Storyfleet off-host backup is stale or last run failed (Wave C).
set -euo pipefail
STATUS_FILE="${STATUS_FILE:-/opt/autostory/data/runtime/offhost_backup_status.json}"
MAX_AGE_HOURS="${MAX_AGE_HOURS:-36}"

if [[ ! -f "$STATUS_FILE" ]]; then
  echo "STORYFLEET_BACKUP_AGE_FAIL reason=missing_status file=$STATUS_FILE"
  exit 2
fi

python3 - <<PY
import json, sys, datetime
from pathlib import Path
path = Path("$STATUS_FILE")
max_h = float("$MAX_AGE_HOURS")
data = json.loads(path.read_text())
state = data.get("state")
finished = data.get("finished_at_utc") or ""
off_host = data.get("off_host")
if state != "ok":
    print(f"STORYFLEET_BACKUP_AGE_FAIL reason=state state={state} message={data.get('message')}")
    sys.exit(2)
if off_host not in ("ok", "dry_run", "disabled"):
    # disabled is only OK if operator intentionally disabled; treat unknown/failed as fail
    if off_host != "disabled":
        print(f"STORYFLEET_BACKUP_AGE_FAIL reason=off_host off_host={off_host}")
        sys.exit(2)
try:
    ts = datetime.datetime.strptime(finished, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
except Exception as e:
    print(f"STORYFLEET_BACKUP_AGE_FAIL reason=bad_timestamp error={e}")
    sys.exit(2)
age_h = (datetime.datetime.now(datetime.timezone.utc) - ts).total_seconds() / 3600.0
if age_h > max_h:
    print(f"STORYFLEET_BACKUP_AGE_FAIL reason=stale age_hours={age_h:.1f} max={max_h} finished={finished}")
    sys.exit(2)
print(f"STORYFLEET_BACKUP_AGE_OK age_hours={age_h:.2f} finished={finished} off_host={off_host} bundle={data.get('bundle')}")
PY
