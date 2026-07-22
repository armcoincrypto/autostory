#!/usr/bin/env bash
# STORYFLEET scheduler singleton: one process per host via flock.
# Resolves release/workdir from this script location (immutable-release safe).
set -euo pipefail

RELEASE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK="${AUTOSTORY_SCHEDULER_LOCK_PATH:-/tmp/autostory-scheduler.lock}"
cd "$RELEASE_ROOT" || exit 1

exec 200>>"$LOCK"
if ! flock -n 200; then
  echo "autostory_scheduler_singleton_lock_held lock_path=${LOCK} event=scheduler_singleton_denied" >&2
  exit 1
fi

exec /opt/autostory/venv/bin/python main.py scheduler
