#!/bin/bash
# Deploy latest code to server and restart services.
# Run from project root: ./deploy/update-server.sh
# Passwordless: run ./deploy/ssh-setup.sh once (then deploy won't ask for password).
# Or: bash deploy/update-server.sh

set -e
SERVER="${1:-root@207.180.212.142}"

echo "Deploying to $SERVER ..."
rsync -avz --exclude 'venv' --exclude '__pycache__' --exclude '.git' --exclude '*.pyc' --exclude 'data' \
  . "$SERVER:/tmp/autostory-upload/"

echo "Installing and restarting services..."
ssh "$SERVER" 'bash -s' << 'REMOTE'
set -e
# Sync upload into /opt/autostory (overwrites files; mv would fail on non-empty dirs)
rsync -av --exclude venv --exclude data --exclude .env /tmp/autostory-upload/ /opt/autostory/
mkdir -p /opt/autostory/scripts
cd /opt/autostory

# Install/update systemd units (required for bot to run without User=storyfleet)
[ -f deploy/autostory-web.service ] && sudo cp -f deploy/autostory-web.service /etc/systemd/system/
sudo cp -f deploy/storyfleet-bot.service /etc/systemd/system/
sudo systemctl daemon-reload

# Restart or start web dashboard on port 8000 (use autostory-web only)
# If port 8000 is held by something other than gunicorn (e.g. uvicorn), free it first
if command -v lsof &>/dev/null; then
  pids=$(lsof -t -i:8000 2>/dev/null) || true
  for pid in $pids; do
    [ -z "$pid" ] && break
    cmd=""
    [ -r /proc/"$pid"/cmdline ] && cmd=$(tr '\0' ' ' < /proc/"$pid"/cmdline 2>/dev/null) || true
    if echo "$cmd" | grep -q gunicorn; then
      : # our app, leave it
    else
      echo "Freeing port 8000 from PID $pid (not gunicorn)"
      kill "$pid" 2>/dev/null || true
      sleep 2
    fi
  done
fi
if [ -f /etc/systemd/system/autostory-web.service ]; then
  sudo systemctl restart autostory-web 2>/dev/null || sudo systemctl start autostory-web
  echo "---"
  sudo systemctl status autostory-web --no-pager || true
else
  echo "autostory-web.service not found; copy deploy/autostory-web.service to /etc/systemd/system/"
fi

# Start bot
sudo mkdir -p /var/log/storyfleet
sudo systemctl restart storyfleet-bot
sudo systemctl start storyfleet-bot
echo "---"
sudo systemctl status storyfleet-bot --no-pager || true
echo "---"
# Allow port 8000 in firewall if ufw is active
if command -v ufw &>/dev/null && sudo ufw status 2>/dev/null | grep -q "Status: active"; then
  sudo ufw allow 8000/tcp 2>/dev/null || true
  sudo ufw reload 2>/dev/null || true
  echo "Firewall: port 8000 allowed (ufw)."
fi
echo "Done. Dashboard: http://207.180.212.142:8000/"
REMOTE

echo "Deploy complete."
