#!/usr/bin/env bash
# Shared constants and helpers for deploy_production.sh / rollback_production.sh / status.sh.
# Sourced, not executed directly. Read-only-safe: sourcing this file alone makes no changes.
#
# All paths here reflect the *actually running* production configuration, confirmed by
# read-only `systemctl show` / `cat` inspection on 2026-08-27 -- not the stale committed
# deploy/*.service templates, which hardcode WorkingDirectory=/opt/autostory and do not
# match reality.

set -euo pipefail

if (( BASH_VERSINFO[0] < 4 )); then
  echo "FATAL: this tooling requires bash 4+ (associative arrays). Running under ${BASH_VERSION}." >&2
  echo "On macOS the system /bin/bash is 3.2; install a newer one (e.g. 'brew install bash') and invoke via that bash explicitly for local testing. Production Linux hosts ship bash 4+/5+ by default." >&2
  exit 1
fi

# --- Fixed production facts (confirmed by live inspection, not assumed) ---
# Every path below is overridable via environment variable of the same name, purely so
# this tooling can be exercised end-to-end against a local scratch sandbox (see
# scripts/release/local_sandbox_check.sh) before it is ever pointed at real production
# paths. Production runs (root, no overrides set) always resolve to the real values.
: "${RELEASES_ROOT:=/opt/autostory-releases}"
: "${CURRENT_SYMLINK:=${RELEASES_ROOT}/current}"
: "${SHARED_ENV:=/opt/autostory/.env}"
: "${SHARED_DATA:=/opt/autostory/data}"
: "${SHARED_DB:=${SHARED_DATA}/storyfleet.db}"
: "${SHARED_VENV_PY:=/opt/autostory/venv/bin/python}"

# Dedicated bare mirror this tooling owns, used as the *only* git source for `git archive`.
# Never /opt/autostory itself -- that working checkout is stale/dirty historical state and
# must never be treated as production source (explicit rule).
: "${DEPLOY_MIRROR:=/opt/autostory-releases/.git-mirror}"
: "${GIT_REMOTE_URL:=git@github.com:armcoincrypto/autostory.git}"

SYSTEMD_UNITS=(autostory-web autostory-scheduler autostory-readiness-worker)
: "${OVERRIDE_CONF_WEB:=/etc/systemd/system/autostory-web.service.d/override.conf}"
: "${OVERRIDE_CONF_SCHEDULER:=/etc/systemd/system/autostory-scheduler.service.d/override.conf}"
: "${OVERRIDE_CONF_READINESS:=/etc/systemd/system/autostory-readiness-worker.service.d/override.conf}"
declare -A OVERRIDE_CONF=(
  [autostory-web]="$OVERRIDE_CONF_WEB"
  [autostory-scheduler]="$OVERRIDE_CONF_SCHEDULER"
  [autostory-readiness-worker]="$OVERRIDE_CONF_READINESS"
)

: "${AUDIT_ROOT:=/var/lib/server-ops/audits}"
: "${LOCK_FILE:=/var/lock/autostory-production-deploy.lock}"

HEALTH_URL_BASE="https://ex.zellotex.com"
HEALTH_PATHS=(/login /stories /accounts)

# --- Logging ---
log()  { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
fail() { log "ABORT: $*" >&2; exit 1; }

require_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    fail "must run as root (sudo) -- this tool touches systemd units and /opt/autostory-releases"
  fi
}

# --- Read-only facts (safe to call from status.sh too) ---
current_release_of() {
  # $1 = systemd unit name
  # Wave F: autostory-web may use WorkingDirectory under data/runtime while the
  # immutable release is carried by PYTHONPATH. Prefer a path under RELEASES_ROOT.
  local u="$1" wd pp
  wd="$(systemctl show "$u" -p WorkingDirectory --value 2>/dev/null || true)"
  if [[ -n "$wd" && "$wd" == "${RELEASES_ROOT}"/* ]]; then
    readlink -f "$wd" 2>/dev/null || printf '%s\n' "$wd"
    return 0
  fi
  pp="$(systemctl show "$u" -p Environment --value 2>/dev/null | tr ' ' '\n' | sed -n 's/^PYTHONPATH=//p' | head -1 || true)"
  if [[ -n "$pp" ]]; then
    readlink -f "$pp" 2>/dev/null || printf '%s\n' "$pp"
    return 0
  fi
  # Last resort: WorkingDirectory even if outside releases (legacy)
  printf '%s\n' "$wd"
}

unit_active_state() {
  systemctl is-active "$1" 2>/dev/null || echo "unknown"
}

unit_nrestarts() {
  systemctl show "$1" -p NRestarts --value 2>/dev/null || echo "?"
}

story_count() { sqlite3 "$SHARED_DB" "SELECT COUNT(*) FROM stories;"; }
message_deliveries_count() { sqlite3 "$SHARED_DB" "SELECT COUNT(*) FROM message_deliveries;"; }
active_campaign_count() { sqlite3 "$SHARED_DB" "SELECT COUNT(*) FROM auto_story_campaigns WHERE status='active';"; }
account_lock_count() { sqlite3 "$SHARED_DB" "SELECT COUNT(*) FROM auto_story_account_locks;"; }

# --- Deployment mirror maintenance (bare, no working tree -> nothing to be "dirty") ---
ensure_deploy_mirror() {
  if [[ ! -d "$DEPLOY_MIRROR" ]]; then
    log "deploy mirror missing, cloning bare mirror to $DEPLOY_MIRROR"
    git clone --mirror "$GIT_REMOTE_URL" "$DEPLOY_MIRROR"
  else
    log "fetching deploy mirror"
    git --git-dir="$DEPLOY_MIRROR" fetch --prune origin '+refs/heads/*:refs/heads/*' '+refs/tags/*:refs/tags/*'
  fi
}

sha_exists_in_mirror() {
  git --git-dir="$DEPLOY_MIRROR" cat-file -e "${1}^{commit}" 2>/dev/null
}
