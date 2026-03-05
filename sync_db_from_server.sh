#!/usr/bin/env bash
# Copy database (and optionally .env) from the server so local dev has the same data.
# Run this before run_local.sh when you want accounts, targets, deliveries from the server.
#
# Usage:
#   ./sync_db_from_server.sh [user@host] [--env]
#
#   --env    Also copy .env (Telegram API credentials) so Add Account and Send test now work
#
# Example:
#   ./sync_db_from_server.sh root@207.180.212.142
#   ./sync_db_from_server.sh root@207.180.212.142 --env

DEFAULT_HOST="root@207.180.212.142"
HOST="$DEFAULT_HOST"
SYNC_ENV=false
for arg in "$@"; do
  if [[ "$arg" == "--env" ]]; then
    SYNC_ENV=true
  else
    HOST="$arg"
  fi
done

REMOTE_BASE="/opt/autostory"
REMOTE_DB="$REMOTE_BASE/data/app.db"
LOCAL_DIR="$(dirname "$0")"
LOCAL_DB="$LOCAL_DIR/data/app.db"

mkdir -p "$LOCAL_DIR/data"
echo "Syncing from $HOST..."
echo "  DB: $REMOTE_DB → $LOCAL_DB"
scp "$HOST:$REMOTE_DB" "$LOCAL_DB"

if $SYNC_ENV; then
  echo "  .env: $REMOTE_BASE/.env → $LOCAL_DIR/.env"
  scp "$HOST:$REMOTE_BASE/.env" "$LOCAL_DIR/.env"
  echo "  (Add Account and Send test now will now work)"
fi

echo "Done. Run ./run_local.sh and open http://127.0.0.1:5001/"
