#!/usr/bin/env bash
# STORYFLEET host preflight — run before enabling/restarting storyfleet-bot (requires User=storyfleet).
# Idempotent: creates system user and directories if missing.
# chown -R applies ONLY to ${APP_DIR}/data and ${LOG_DIR} — never the whole app or venv tree.
#
# Usage (as root on the server):
#   sudo bash /opt/autostory/deploy/host-preflight.sh
#
# Optional overrides (preserve env with -E):
#   sudo -E env APP_DIR=/opt/autostory LOG_DIR=/var/log/storyfleet STORYFLEET_USER=storyfleet \
#     bash /opt/autostory/deploy/host-preflight.sh

set -euo pipefail

STORYFLEET_USER="${STORYFLEET_USER:-storyfleet}"
APP_DIR="${APP_DIR:-/opt/autostory}"
LOG_DIR="${LOG_DIR:-/var/log/storyfleet}"

if [[ "${EUID:-0}" -ne 0 ]]; then
  echo "host-preflight: must run as root (use sudo)." >&2
  exit 1
fi

if ! id "${STORYFLEET_USER}" &>/dev/null; then
  useradd \
    --system \
    --home-dir "${APP_DIR}" \
    --no-create-home \
    --shell /usr/sbin/nologin \
    "${STORYFLEET_USER}"
  echo "host-preflight: created system user ${STORYFLEET_USER}"
else
  echo "host-preflight: user ${STORYFLEET_USER} already exists"
fi

mkdir -p "${APP_DIR}/data" "${LOG_DIR}"

chown -R "${STORYFLEET_USER}:${STORYFLEET_USER}" "${APP_DIR}/data" "${LOG_DIR}"

echo "host-preflight: ownership set for ${APP_DIR}/data and ${LOG_DIR}"
echo "host-preflight: ok"
