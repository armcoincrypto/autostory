#!/usr/bin/env bash
# Storyfleet off-host DR backup (Wave C).
# Creates an encrypted recovery bundle and replicates it off-host.
# Secrets are never printed. Fail closed when off-host is enabled but unreachable.
set -euo pipefail
IFS=$'\n\t'

OFFSITE_ENV_FILE="${OFFSITE_ENV_FILE:-/etc/autostory/offsite-backup.env}"
LOCAL_BACKUP_ROOT="${LOCAL_BACKUP_ROOT:-/var/lib/storyfleet/backups}"
STATUS_FILE="${STATUS_FILE:-/opt/autostory/data/runtime/offhost_backup_status.json}"
DB_PATH="${DB_PATH:-/opt/autostory/data/storyfleet.db}"
ENV_PATH="${ENV_PATH:-/opt/autostory/.env}"
SESSION_KEYS_ENV="${SESSION_KEYS_ENV:-/etc/autostory/telegram-session-keys.env}"
SESSIONS_KEY_FILE="${SESSIONS_KEY_FILE:-/opt/autostory/data/sessions/.key}"
MEDIA_DIR="${MEDIA_DIR:-/opt/autostory/data/media}"
RELEASE_CURRENT="${RELEASE_CURRENT:-/opt/autostory-releases/current}"
LOCAL_KEEP="${LOCAL_KEEP:-3}"
DRY_RUN="${DRY_RUN:-0}"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "ERROR: $*"; write_status failed "$*"; exit 1; }

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

env_bool() {
  local v
  v="$(trim "${1:-}")"
  [[ "$v" == "1" || "$v" == "true" || "$v" == "TRUE" || "$v" == "yes" || "$v" == "YES" ]]
}

write_status() {
  local state="$1" msg="${2:-}"
  mkdir -p "$(dirname "$STATUS_FILE")"
  local finished
  finished="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  umask 077
  cat >"$STATUS_FILE" <<EOF
{
  "state": "$(printf '%s' "$state" | sed 's/"/\\"/g')",
  "finished_at_utc": "$finished",
  "message": "$(printf '%s' "$msg" | sed 's/"/\\"/g' | tr '\n' ' ')",
  "bundle": "$(printf '%s' "${BUNDLE_NAME:-}" | sed 's/"/\\"/g')",
  "off_host": "$(printf '%s' "${OFF_HOST_RESULT:-unknown}" | sed 's/"/\\"/g')",
  "encrypted": true,
  "local_dir": "$(printf '%s' "${STAMP_DIR:-}" | sed 's/"/\\"/g')"
}
EOF
  # Wave F/I: storyfleet must read status for owner Dashboard (group-readable, not world).
  chgrp storyfleet "$STATUS_FILE" 2>/dev/null || true
  chmod 640 "$STATUS_FILE" 2>/dev/null || true
}

require_file() {
  [[ -f "$1" ]] || die "required file missing: $1"
}

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BUNDLE_NAME="storyfleet-dr-${STAMP}.tar.gpg"
STAMP_DIR="${LOCAL_BACKUP_ROOT}/${STAMP}"
WORK="${STAMP_DIR}/payload"
OFF_HOST_RESULT="skipped"

mkdir -p "$LOCAL_BACKUP_ROOT"
umask 077
mkdir -p "$WORK"/{db,config,keys,media,systemd,release,meta}

log "storyfleet_offhost_backup_start stamp=$STAMP"

require_file "$DB_PATH"
require_file "$ENV_PATH"
require_file "$SESSION_KEYS_ENV"
require_file "$SESSIONS_KEY_FILE"
command -v sqlite3 >/dev/null || die "sqlite3 required"
command -v gpg >/dev/null || die "gpg required"
command -v tar >/dev/null || die "tar required"
command -v sha256sum >/dev/null || die "sha256sum required"

# --- DB consistent snapshot ---
log "sqlite_backup"
sqlite3 "$DB_PATH" ".backup '${WORK}/db/storyfleet.db'"
sqlite3 "${WORK}/db/storyfleet.db" "PRAGMA integrity_check;" | grep -qx ok \
  || die "sqlite integrity_check failed on backup copy"
ACCOUNT_SESSIONS="$(sqlite3 "${WORK}/db/storyfleet.db" "SELECT COUNT(*) FROM accounts WHERE session_string IS NOT NULL AND length(session_string)>0;")"
log "db_accounts_with_session_string=${ACCOUNT_SESSIONS}"

# --- Config + keys (must restore together) ---
cp -a "$ENV_PATH" "$WORK/config/autostory.env"
cp -a "$SESSION_KEYS_ENV" "$WORK/keys/telegram-session-keys.env"
cp -a "$SESSIONS_KEY_FILE" "$WORK/keys/sessions.key"
if [[ -f /etc/autostory/autostory.env ]]; then
  cp -a /etc/autostory/autostory.env "$WORK/config/etc-autostory.env"
fi

# --- Media (bounded operational uploads; not the 12G local backup tree) ---
if [[ -d "$MEDIA_DIR" ]]; then
  log "media_copy"
  # Exclude obvious junk; keep owner media.
  rsync -a --delete \
    --exclude='.gitkeep' \
    --exclude='canary_*' \
    --exclude='audit_*' \
    "$MEDIA_DIR/" "$WORK/media/" || die "media copy failed"
fi

# --- systemd unit definitions ---
for u in autostory-web.service autostory-scheduler.service autostory-readiness-worker.service \
         autostory-fleet-matrix-refresh.service autostory-fleet-matrix-refresh.timer; do
  if [[ -f "/etc/systemd/system/$u" ]]; then
    cp -a "/etc/systemd/system/$u" "$WORK/systemd/$u"
  fi
done
# Drop-in overrides if present
if [[ -d /etc/systemd/system/autostory-web.service.d ]]; then
  mkdir -p "$WORK/systemd/autostory-web.service.d"
  cp -a /etc/systemd/system/autostory-web.service.d/. "$WORK/systemd/autostory-web.service.d/" || true
fi
for svc in scheduler readiness-worker; do
  if [[ -d "/etc/systemd/system/autostory-${svc}.service.d" ]]; then
    mkdir -p "$WORK/systemd/autostory-${svc}.service.d"
    cp -a "/etc/systemd/system/autostory-${svc}.service.d/." "$WORK/systemd/autostory-${svc}.service.d/" || true
  fi
done

# --- Release metadata / deploy pointers ---
if [[ -L "$RELEASE_CURRENT" || -d "$RELEASE_CURRENT" ]]; then
  readlink -f "$RELEASE_CURRENT" >"$WORK/release/current_path.txt" || true
  if [[ -f "$RELEASE_CURRENT/RELEASE_MANIFEST.json" ]]; then
    cp -a "$RELEASE_CURRENT/RELEASE_MANIFEST.json" "$WORK/release/RELEASE_MANIFEST.json"
  fi
fi
{
  echo "created_at_utc=$STAMP"
  echo "host=$(hostname -f 2>/dev/null || hostname)"
  echo "db_path=$DB_PATH"
  echo "accounts_with_session_string=$ACCOUNT_SESSIONS"
  echo "WARNING=Do not connect restored Telegram sessions live without draining the primary host (session contention risk)."
  echo "RESTORE=See docs/runbooks/STORYFLEET_DISASTER_RECOVERY.md"
} >"$WORK/meta/MANIFEST.txt"

# --- Package + encrypt ---
ENC_KEY="$(read_env OFFSITE_BACKUP_ENCRYPTION_KEY)"
[[ -n "$(trim "$ENC_KEY")" ]] || die "OFFSITE_BACKUP_ENCRYPTION_KEY missing in $OFFSITE_ENV_FILE"

TAR_PATH="${STAMP_DIR}/storyfleet-dr-${STAMP}.tar"
GPG_PATH="${STAMP_DIR}/${BUNDLE_NAME}"
log "tar_create"
tar -C "$WORK" -cf "$TAR_PATH" .
log "gpg_encrypt"
# Passphrase via FD so it never appears in process argv listing as clearly.
printf '%s' "$ENC_KEY" | gpg --batch --yes --symmetric --cipher-algo AES256 \
  --compress-algo zlib --pinentry-mode loopback --passphrase-fd 0 \
  -o "$GPG_PATH" "$TAR_PATH"
shred -u "$TAR_PATH" 2>/dev/null || rm -f "$TAR_PATH"
# Remove plaintext payload after packaging
rm -rf "$WORK"
sha256sum "$GPG_PATH" | awk '{print $1}' >"${GPG_PATH}.sha256"
chmod 600 "$GPG_PATH" "${GPG_PATH}.sha256"

BUNDLE_BYTES="$(stat -c%s "$GPG_PATH")"
log "bundle_ready bytes=$BUNDLE_BYTES name=$BUNDLE_NAME"

# --- Local retention ---
mapfile -t LOCAL_STAMPS < <(ls -1dt "${LOCAL_BACKUP_ROOT}"/20* 2>/dev/null || true)
if ((${#LOCAL_STAMPS[@]} > LOCAL_KEEP)); then
  for old in "${LOCAL_STAMPS[@]:LOCAL_KEEP}"; do
    log "prune_local $old"
    rm -rf "$old"
  done
fi

# --- Off-host replicate ---
ENABLED="$(read_env OFFSITE_BACKUP_ENABLED)"
if ! env_bool "$ENABLED"; then
  OFF_HOST_RESULT="disabled"
  write_status ok "local_only"
  log "offhost_disabled local_ok=YES"
  exit 0
fi

REMOTE_USER="$(read_env OFFSITE_BACKUP_REMOTE_USER)"
REMOTE_HOST="$(read_env OFFSITE_BACKUP_REMOTE_HOST)"
REMOTE_PATH="$(read_env OFFSITE_BACKUP_REMOTE_PATH)"
SSH_KEY="$(read_env OFFSITE_BACKUP_SSH_KEY)"
RETENTION_DAYS="$(read_env OFFSITE_BACKUP_RETENTION_DAYS)"
RETENTION_DAYS="${RETENTION_DAYS:-30}"

[[ -n "$REMOTE_USER" && -n "$REMOTE_HOST" && -n "$REMOTE_PATH" ]] || die "off-host remote incomplete"
[[ -f "$SSH_KEY" ]] || die "SSH key missing"

SSH=(ssh -i "$SSH_KEY" -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20)
RSYNC_SSH="ssh -i ${SSH_KEY} -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20"
TARGET="${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_PATH%/}/"

log "offhost_mkdir"
"${SSH[@]}" "${REMOTE_USER}@${REMOTE_HOST}" "mkdir -p '${REMOTE_PATH%/}'"

if [[ "$DRY_RUN" == "1" ]]; then
  log "offhost_dry_run target_host=$REMOTE_HOST"
  OFF_HOST_RESULT="dry_run"
  write_status ok "dry_run"
  exit 0
fi

log "offhost_rsync"
rsync -av --chmod=Fu=rw,Fgo=,Du=rwx,Dgo= \
  -e "$RSYNC_SSH" \
  "$GPG_PATH" "${GPG_PATH}.sha256" \
  "$TARGET" || die "off-host rsync failed"
OFF_HOST_RESULT="ok"

# Remote retention by age
log "offhost_retention_days=$RETENTION_DAYS"
"${SSH[@]}" "${REMOTE_USER}@${REMOTE_HOST}" \
  "find '${REMOTE_PATH%/}' -maxdepth 1 -type f -name 'storyfleet-dr-*.tar.gpg' -mtime +${RETENTION_DAYS} -delete; find '${REMOTE_PATH%/}' -maxdepth 1 -type f -name 'storyfleet-dr-*.tar.gpg.sha256' -mtime +${RETENTION_DAYS} -delete; ls -1t '${REMOTE_PATH%/}'/storyfleet-dr-*.tar.gpg 2>/dev/null | head -5" \
  >/tmp/storyfleet-offhost-ls.txt || die "off-host retention/list failed"
REMOTE_COUNT="$(grep -c 'storyfleet-dr-' /tmp/storyfleet-offhost-ls.txt || true)"
rm -f /tmp/storyfleet-offhost-ls.txt
log "offhost_ok remote_listed=${REMOTE_COUNT}"

write_status ok "off_host_replicated"
log "STORYFLEET_OFFHOST_BACKUP_PASS stamp=$STAMP bytes=$BUNDLE_BYTES"
