#!/bin/bash
# Run ONCE on the server to open port 8000 so the dashboard is reachable from the internet.
# Option A - From your Mac, run:  ssh root@207.180.212.142 'bash -s' < deploy/open-port-8000.sh
# Option B - SSH into the server, then:  bash /opt/autostory/deploy/open-port-8000.sh

set -e
echo "Checking port 8000..."
if ss -tlnp 2>/dev/null | grep -q ':8000 '; then
  echo "OK: Something is listening on port 8000."
else
  echo "WARN: Nothing listening on 8000. Is autostory-web running? sudo systemctl status autostory-web"
fi

echo ""
echo "Opening port 8000 in firewall..."
if command -v ufw &>/dev/null; then
  if sudo ufw status 2>/dev/null | grep -q "Status: active"; then
    sudo ufw allow 8000/tcp
    sudo ufw reload
    echo "Done. Port 8000 allowed (ufw)."
  else
    echo "ufw is installed but not active. Enable with: sudo ufw enable"
  fi
elif command -v firewall-cmd &>/dev/null; then
  sudo firewall-cmd --add-port=8000/tcp --permanent
  sudo firewall-cmd --reload
  echo "Done. Port 8000 allowed (firewalld)."
else
  echo "No ufw or firewalld found. If you use another firewall or a cloud security group, allow inbound TCP port 8000."
fi

echo ""
echo "Try from your browser: http://207.180.212.142:8000/"
