#!/bin/bash
# Deploy latest code to server and restart services.
# Run from project root: ./deploy/update-server.sh
# Or: bash deploy/update-server.sh
#
# Remote order: rsync (clean staging) → rsync into /opt/autostory (mirror app tree, never data/venv/.env)
#   → host-preflight → systemd units → daemon-reload → restart web → restart bot.

set -e
SERVER="${1:-root@207.180.212.142}"

echo "Deploying to $SERVER ..."
# Staging mirror: drop files removed from repo so they are not re-copied to prod.
# Never ship local .env to the server (defense in depth; prod .env stays on host).
rsync -avz --delete \
  --exclude 'venv/' \
  --exclude '__pycache__/' \
  --exclude '.git/' \
  --exclude '*.pyc' \
  --exclude 'data/' \
  --exclude '.env' \
  --exclude '.env.*' \
  ./ "${SERVER}:/tmp/autostory-upload/"

echo "Installing and restarting services..."
ssh "$SERVER" 'bash -s' << 'REMOTE'
set -e
# Mirror app tree into /opt/autostory. Protected paths excluded so --delete never touches them.
# .env / venv / data / logs are only on the server and are not removed.
rsync -av --delete \
  --exclude 'data/' \
  --exclude 'venv/' \
  --exclude '.env' \
  --exclude '.env.*' \
  --exclude '.git/' \
  /tmp/autostory-upload/ /opt/autostory/

cd /opt/autostory

if [[ ! -f deploy/host-preflight.sh ]]; then
  echo "deploy/update-server: deploy/host-preflight.sh missing under /opt/autostory after sync." >&2
  echo "Refusing to restart the bot — install repo deploy/ then re-run deploy." >&2
  exit 1
fi
sudo bash deploy/host-preflight.sh

[ -f deploy/autostory-web.service ] && sudo cp -f deploy/autostory-web.service /etc/systemd/system/
sudo cp -f deploy/storyfleet-bot.service /etc/systemd/system/
sudo systemctl daemon-reload

if systemctl is-active --quiet autostory-web 2>/dev/null; then
  sudo systemctl restart autostory-web
elif systemctl is-active --quiet storyfleet-dashboard 2>/dev/null; then
  sudo systemctl restart storyfleet-dashboard
else
  if sudo systemctl start autostory-web 2>/dev/null; then
    echo "Started autostory-web."
  elif sudo systemctl start storyfleet-dashboard 2>/dev/null; then
    echo "Started storyfleet-dashboard."
  else
    echo "No known web service found; ensure gunicorn is running on 8000."
  fi
fi

sudo systemctl restart storyfleet-bot
sudo systemctl start storyfleet-bot
echo "---"
sudo systemctl status storyfleet-bot --no-pager || true
echo "---"
echo "Done."
REMOTE

echo "Deploy complete."
