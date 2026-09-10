#!/usr/bin/env bash
# Install Storyfleet ops health oneshot + timer (Wave L).
set -euo pipefail

UNIT_DIR=/etc/systemd/system
REL="$(readlink -f /opt/autostory-releases/current)"
PY="${STORYFLEET_PYTHON:-/opt/autostory/venv/bin/python}"

install -d -m 0755 /opt/autostory/data/runtime

cat >"${UNIT_DIR}/storyfleet-ops-health.service" <<EOF
[Unit]
Description=Storyfleet consolidated ops health check (read-only)
After=network.target autostory-web.service

[Service]
Type=oneshot
User=root
Group=root
Environment=PYTHONPATH=${REL}
# Prefer live release path at ExecStart time via current symlink:
ExecStart=/bin/bash -lc 'REL=\$(readlink -f /opt/autostory-releases/current); export PYTHONPATH="\$REL"; exec ${PY} "\$REL/scripts/ops/storyfleet_ops_health_check.py" --quiet'
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7

[Install]
WantedBy=multi-user.target
EOF

cat >"${UNIT_DIR}/storyfleet-ops-health.timer" <<'EOF'
[Unit]
Description=Run Storyfleet ops health every 15 minutes

[Timer]
OnBootSec=2m
OnUnitActiveSec=15m
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now storyfleet-ops-health.timer
systemctl start storyfleet-ops-health.service || true
systemctl --no-pager --full status storyfleet-ops-health.timer | head -20
echo "Installed storyfleet-ops-health.timer"
