#!/usr/bin/env bash
# Run STORYFLEET Dashboard locally on your Mac for quick UI testing.
# No deploy needed: edit code → save → browser auto-refreshes.
#
# Usage:
#   ./run_local.sh
#
# Then open: http://127.0.0.1:5000/

set -e
cd "$(dirname "$0")"

# Use venv if present
if [ -d "venv" ]; then
  source venv/bin/activate
fi

# Ensure data dir exists
mkdir -p data data/sessions data/media data/logs

# Local dev: debug + reload on file changes
# Port 5001 to avoid conflict with macOS AirPlay Receiver on 5000 (override: PORT=5002 ./run_local.sh)
export ENVIRONMENT=development
export DASHBOARD_DEBUG=true
export DASHBOARD_HOST=0.0.0.0
export DASHBOARD_PORT="${PORT:-5001}"

# Proxy scheduler API to server when set. Unset for local execution: DASHBOARD_RUN_NOW_PROXY_URL= ./run_local.sh
# (Only default when completely unset; empty string means "no proxy")
if [ -z "${DASHBOARD_RUN_NOW_PROXY_URL+x}" ]; then
  export DASHBOARD_RUN_NOW_PROXY_URL="http://207.180.212.142:5000"
fi

echo "Dashboard: http://127.0.0.1:${DASHBOARD_PORT}/"
if [ -n "$DASHBOARD_RUN_NOW_PROXY_URL" ]; then
  echo "Scheduler API proxied to server"
else
  echo "Running locally (Send Test Now uses your DB + sessions)"
fi
echo "Ctrl+C to stop"
echo ""
echo "To run the Random Scheduler (auto-send at random times):"
echo "  In another terminal: python main.py scheduler"
echo ""
python main.py dashboard
