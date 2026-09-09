#!/usr/bin/env bash
# Disposable restore drill for Storyfleet DR bundles (Wave C).
# NEVER starts Telegram clients / never points production services at restored data.
set -euo pipefail
IFS=$'\n\t'

OFFSITE_ENV_FILE="${OFFSITE_ENV_FILE:-/etc/autostory/offsite-backup.env}"
RESTORE_ROOT="${RESTORE_ROOT:-/var/tmp/storyfleet-dr-restore}"
SOURCE_BUNDLE="${1:-}"
PULL_LATEST="${PULL_LATEST:-0}"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "ERROR: $*"; exit 1; }

trim() {
  local s="${1:-}"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "$s"
}

strip_wrapping_quotes() {
  local s
  s="$(trim "${1:-}")"
  if [[ ( "$s" == \"*\" && "$s" == *\" ) || ( "$s" == \'*\' && "$s" == *\' ) ]]; then
    s="${s:1:${#s}-2}"
  fi
  printf '%s' "$s"
}

read_env() {
  local key="$1" line val
  if [[ -n "${!key:-}" ]]; then
    printf '%s' "${!key}"
    return 0
  fi
  [[ -f "$OFFSITE_ENV_FILE" ]] || { printf ''; return 0; }
  line="$(grep -m1 -E "^${key}=" "$OFFSITE_ENV_FILE" 2>/dev/null || true)"
  [[ -n "${line:-}" ]] || { printf ''; return 0; }
  val="${line#*=}"
  strip_wrapping_quotes "$val"
}

ENC_KEY="$(read_env OFFSITE_BACKUP_ENCRYPTION_KEY)"
[[ -n "$(trim "$ENC_KEY")" ]] || die "OFFSITE_BACKUP_ENCRYPTION_KEY missing"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="${RESTORE_ROOT}/${STAMP}"
umask 077
mkdir -p "$DEST"
chmod 700 "$RESTORE_ROOT" "$DEST"

if [[ "$PULL_LATEST" == "1" ]]; then
  REMOTE_USER="$(read_env OFFSITE_BACKUP_REMOTE_USER)"
  REMOTE_HOST="$(read_env OFFSITE_BACKUP_REMOTE_HOST)"
  REMOTE_PATH="$(read_env OFFSITE_BACKUP_REMOTE_PATH)"
  SSH_KEY="$(read_env OFFSITE_BACKUP_SSH_KEY)"
  [[ -n "$REMOTE_USER" && -n "$REMOTE_HOST" && -n "$REMOTE_PATH" && -f "$SSH_KEY" ]] || die "off-host config incomplete for pull"
  LATEST="$(ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=20 \
    "${REMOTE_USER}@${REMOTE_HOST}" "ls -1t '${REMOTE_PATH%/}'/storyfleet-dr-*.tar.gpg 2>/dev/null | head -1")"
  [[ -n "$LATEST" ]] || die "no remote bundles found"
  BASE="$(basename "$LATEST")"
  log "pulling_remote $BASE"
  rsync -av -e "ssh -i ${SSH_KEY} -o BatchMode=yes -o ConnectTimeout=20" \
    "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_PATH%/}/${BASE}" \
    "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_PATH%/}/${BASE}.sha256" \
    "$DEST/" || die "pull failed"
  SOURCE_BUNDLE="${DEST}/${BASE}"
fi

if [[ -z "$SOURCE_BUNDLE" ]]; then
  # Default: newest local encrypted bundle
  SOURCE_BUNDLE="$(ls -1t /var/lib/storyfleet/backups/*/storyfleet-dr-*.tar.gpg 2>/dev/null | head -1 || true)"
fi
[[ -n "$SOURCE_BUNDLE" && -f "$SOURCE_BUNDLE" ]] || die "bundle not found (pass path or PULL_LATEST=1)"

log "restore_drill_start bundle=$(basename "$SOURCE_BUNDLE") dest=$DEST"

if [[ -f "${SOURCE_BUNDLE}.sha256" ]]; then
  EXPECTED="$(tr -d ' \n\r' <"${SOURCE_BUNDLE}.sha256" | awk '{print $1}')"
  ACTUAL="$(sha256sum "$SOURCE_BUNDLE" | awk '{print $1}')"
  [[ "$EXPECTED" == "$ACTUAL" ]] || die "sha256 mismatch"
  log "sha256_ok"
fi

TAR_OUT="${DEST}/payload.tar"
printf '%s' "$ENC_KEY" | gpg --batch --yes --decrypt --pinentry-mode loopback --passphrase-fd 0 \
  -o "$TAR_OUT" "$SOURCE_BUNDLE" || die "gpg decrypt failed"

mkdir -p "${DEST}/payload"
tar -C "${DEST}/payload" -xf "$TAR_OUT"
shred -u "$TAR_OUT" 2>/dev/null || rm -f "$TAR_OUT"

DB="${DEST}/payload/db/storyfleet.db"
[[ -f "$DB" ]] || die "restored DB missing"
INTEGRITY="$(sqlite3 "$DB" 'PRAGMA integrity_check;')"
[[ "$INTEGRITY" == "ok" ]] || die "integrity_check=$INTEGRITY"

ACCT="$(sqlite3 "$DB" "SELECT COUNT(*) FROM accounts;")"
WITH_SESS="$(sqlite3 "$DB" "SELECT COUNT(*) FROM accounts WHERE session_string IS NOT NULL AND length(session_string)>0;")"
[[ -f "${DEST}/payload/config/autostory.env" ]] || die "config restore missing"
[[ -f "${DEST}/payload/keys/telegram-session-keys.env" ]] || die "session keys env missing"
[[ -f "${DEST}/payload/keys/sessions.key" ]] || die "sessions.key missing"
[[ -s "${DEST}/payload/keys/sessions.key" ]] || die "sessions.key empty"
[[ -f "${DEST}/payload/keys/telegram-session-keys.env" ]] || die "telegram-session-keys missing"
# Prove key material is readable without printing secrets
KEY_BYTES="$(wc -c <"${DEST}/payload/keys/sessions.key" | tr -d ' ')"
ENV_KEYS="$(grep -cE '^[A-Z0-9_]+=' "${DEST}/payload/keys/telegram-session-keys.env" || true)"
[[ "$KEY_BYTES" -gt 10 ]] || die "sessions.key too small"
[[ "$ENV_KEYS" -ge 1 ]] || die "session encryption env empty"

# Application import sanity: Python can open restored DB schema (no Telegram connect)
python3 - <<PY
import sqlite3
con = sqlite3.connect("$DB")
cur = con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY 1")
tables = [r[0] for r in cur.fetchall()]
need = {"accounts", "scheduled_jobs", "owner_dm_intents", "dashboard_users"}
missing = sorted(need - set(tables))
if missing:
    raise SystemExit(f"missing tables: {missing}")
print("schema_ok tables=", len(tables))
con.close()
PY

EVIDENCE="${DEST}/RESTORE_EVIDENCE.txt"
{
  echo "STORYFLEET_RESTORE_DRILL_PASS"
  echo "finished_at_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "bundle=$(basename "$SOURCE_BUNDLE")"
  echo "dest=$DEST"
  echo "integrity_check=ok"
  echo "accounts=$ACCT"
  echo "accounts_with_session_string=$WITH_SESS"
  echo "sessions_key_bytes=$KEY_BYTES"
  echo "session_env_keys=$ENV_KEYS"
  echo "telegram_live_connect=SKIPPED_BY_DESIGN"
  echo "NOTE=Dispose this directory after review: rm -rf $DEST"
} | tee "$EVIDENCE"

chmod -R go-rwx "$DEST"
log "STORYFLEET_RESTORE_DRILL_PASS dest=$DEST"
