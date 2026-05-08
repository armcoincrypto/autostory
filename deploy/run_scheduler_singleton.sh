#!/usr/bin/env bash
# STORYFLEET scheduler singleton: one process per host via flock.
# Lock is released automatically when the Python process exits (fd closed).
# stderr message is picked up by systemd journal if a duplicate start is attempted.
set -euo pipefail

LOCK="${AUTOSTORY_SCHEDULER_LOCK_PATH:-/tmp/autostory-scheduler.lock}"
cd /opt/autostory || exit 1

exec 200>>"$LOCK"
if ! flock -n 200; then
  echo "autostory_scheduler_singleton_lock_held lock_path=${LOCK} event=scheduler_singleton_denied" >&2
  exit 1
fi

exec /opt/autostory/venv/bin/python main.py scheduler
