#!/usr/bin/env bash
# Install Storyfleet off-host backup systemd units from a release tree.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
UNIT_DIR="${ROOT}/deploy/systemd"

install -d -m 0755 /etc/autostory
if [[ ! -f /etc/autostory/offsite-backup.env ]]; then
  echo "Missing /etc/autostory/offsite-backup.env — copy from deploy/systemd/offsite-backup.env.example and fill secrets."
  exit 1
fi
chmod 600 /etc/autostory/offsite-backup.env

install -m 0644 "$UNIT_DIR/storyfleet-offhost-backup.service" /etc/systemd/system/
install -m 0644 "$UNIT_DIR/storyfleet-offhost-backup.timer" /etc/systemd/system/
install -m 0644 "$UNIT_DIR/storyfleet-backup-age-check.service" /etc/systemd/system/
install -m 0644 "$UNIT_DIR/storyfleet-backup-age-check.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now storyfleet-offhost-backup.timer
systemctl enable --now storyfleet-backup-age-check.timer
systemctl status --no-pager storyfleet-offhost-backup.timer storyfleet-backup-age-check.timer || true
echo "STORYFLEET_OFFHOST_BACKUP_UNITS_INSTALLED"
