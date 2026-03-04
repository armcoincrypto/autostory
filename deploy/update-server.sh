#!/bin/bash
# Deploy latest code to server and restart services.
# Run from project root: ./deploy/update-server.sh
# Or: bash deploy/update-server.sh

set -e
SERVER="${1:-root@207.180.212.142}"

echo "Deploying to $SERVER ..."
rsync -avz --exclude 'venv' --exclude '__pycache__' --exclude '.git' --exclude '*.pyc' --exclude 'data' \
  . "$SERVER:/tmp/autostory-upload/"

echo "Installing and restarting services..."
ssh "$SERVER" 'bash -s' << 'REMOTE'
set -e
cp -r /tmp/autostory-upload/* /opt/autostory/
cd /opt/autostory

# Install/update systemd units (required for bot to run without User=storyfleet)
[ -f deploy/autostory-web.service ] && sudo cp -f deploy/autostory-web.service /etc/systemd/system/
sudo cp -f deploy/storyfleet-bot.service /etc/systemd/system/
sudo systemctl daemon-reload

# Restart or start web dashboard (port 8000)
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

# Start bot
sudo mkdir -p /var/log/storyfleet
sudo systemctl restart storyfleet-bot
sudo systemctl start storyfleet-bot
echo "---"
sudo systemctl status storyfleet-bot --no-pager || true
echo "---"
echo "Done. Dashboard: http://207.180.212.142:8000/"
REMOTE

echo "Deploy complete."
