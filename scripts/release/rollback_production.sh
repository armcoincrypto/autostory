#!/usr/bin/env bash
# Roll AutoStory production back to a previously-known-good immutable release.
# Code rollback ONLY -- never touches the database. A DB backup made by a prior
# deploy_production.sh run is a separate, deliberate, manual operation if a schema
# change specifically requires it.
#
# Usage: sudo scripts/release/rollback_production.sh <previous-release-path>
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_deploy_lib.sh
source "$HERE/_deploy_lib.sh"

PREV_RELEASE="${1:?usage: $(basename "$0") <previous-release-path>}"
require_root

[[ -d "$PREV_RELEASE" ]] || fail "rollback target does not exist: $PREV_RELEASE"
[[ -f "$PREV_RELEASE/RELEASE_MANIFEST.json" ]] || log "WARNING: $PREV_RELEASE has no RELEASE_MANIFEST.json -- proceeding anyway, but this is not a release built by this tooling"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
AUDIT_DIR="${AUDIT_ROOT}/autostory-rollback-${STAMP}"
mkdir -p "$AUDIT_DIR"

ACTIVE_CAMPAIGNS="$(active_campaign_count)"
log "active AutoStory campaigns at rollback time: $ACTIVE_CAMPAIGNS"
if [[ "$ACTIVE_CAMPAIGNS" -gt 0 ]]; then
  log "WARNING: rolling back while $ACTIVE_CAMPAIGNS AutoStory campaign(s) are active."
  log "WARNING: rollback proceeds anyway -- refusing an emergency rollback because of campaign state would leave a broken deploy live, which is worse. Investigate the campaign(s) separately after the code rollback is confirmed healthy."
fi

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  fail "another deployment/rollback appears to be in progress (lock held: $LOCK_FILE)"
fi
log "rollback lock acquired: $LOCK_FILE"

CHANGED_UNITS=()
BACKUP_CONF=()

for u in "${SYSTEMD_UNITS[@]}"; do
  conf="${OVERRIDE_CONF[$u]}"
  old_release="$(current_release_of "$u")"
  [[ -n "$old_release" ]] || fail "cannot determine current WorkingDirectory for $u"

  bkp="${AUDIT_DIR}/$(basename "$conf").${u}.pre-rollback.bak"
  cp "$conf" "$bkp"
  BACKUP_CONF+=("$bkp")
  CHANGED_UNITS+=("$u")

  if [[ "$old_release" == "$PREV_RELEASE" ]]; then
    log "$u already at rollback target ($PREV_RELEASE) -- no edit needed"
    continue
  fi
  if ! grep -qF "$old_release" "$conf"; then
    fail "$conf does not contain the expected current release path ($old_release) -- refusing to blind-edit"
  fi
  sed -i.orig-unused "s#${old_release}#${PREV_RELEASE}#g" "$conf"
  rm -f "${conf}.orig-unused"
  log "$u override.conf updated: $old_release -> $PREV_RELEASE"
done

systemctl daemon-reload
HEALTH_OK=1
for u in "${SYSTEMD_UNITS[@]}"; do
  systemctl restart "$u"
  sleep 2
  state="$(unit_active_state "$u")"
  wd="$(current_release_of "$u")"
  echo "$u ActiveState=$state WorkingDirectory=$wd" | tee -a "${AUDIT_DIR}/HEALTH_AFTER.txt"
  if [[ "$state" != "active" || "$wd" != "$PREV_RELEASE" ]]; then
    HEALTH_OK=0
    log "WARNING: $u did not come back healthy on the rollback target (state=$state wd=$wd)"
  fi
done

if [[ "$HEALTH_OK" -eq 1 ]]; then
  ln -sfn "$PREV_RELEASE" "$CURRENT_SYMLINK"
  log "current symlink restored -> $PREV_RELEASE"
else
  log "current symlink left untouched -- not all services healthy on rollback target, needs manual attention"
fi

for p in "${HEALTH_PATHS[@]}"; do
  code="$(curl -fsS -o /dev/null -w '%{http_code}' --max-time 10 "${HEALTH_URL_BASE}${p}" || echo "000")"
  echo "http[$p]=$code" | tee -a "${AUDIT_DIR}/HEALTH_AFTER.txt"
done

flock -u 9
log "rollback lock released"

{
  echo "=== ROLLBACK FINAL REPORT ($(date -u +%Y-%m-%dT%H:%M:%SZ)) ==="
  if [[ "$HEALTH_OK" -eq 1 ]]; then echo AUTOSTORY_ROLLBACK_PASS; else echo AUTOSTORY_ROLLBACK_NEEDS_INVESTIGATION; fi
  echo "TARGET_RELEASE=$PREV_RELEASE"
  echo "ACTIVE_CAMPAIGNS_AT_ROLLBACK=$ACTIVE_CAMPAIGNS"
  echo "DB_NOT_TOUCHED=YES (code rollback only)"
  echo "AUDIT_DIR=$AUDIT_DIR"
} | tee -a "${AUDIT_DIR}/FINAL_REPORT.txt"

[[ "$HEALTH_OK" -eq 1 ]]
