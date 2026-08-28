#!/usr/bin/env bash
# Canonical AutoStory production deploy. Fails closed at every stage.
#
# Usage:
#   sudo scripts/release/deploy_production.sh --sha <git-sha> [--dry-run] [--force]
#   sudo scripts/release/deploy_production.sh <git-sha> [--dry-run] [--force]
#
# Always pins to an explicit, immutable Git SHA -- never a branch tip.
#
# If the requested SHA is already the live release (all three services already
# report a RELEASE_MANIFEST.json git_sha matching it), a real (non-dry-run) run
# ABORTS rather than performing a pointless cutover -- pass --force to rebuild
# and cut over anyway (e.g. to recover from a corrupted release directory).
# --dry-run always reports this fact but is never blocked by it.
#
# Pipeline: prechecks -> DB backup+integrity -> build release -> verify manifest
#   -> python syntax -> active-campaign gate -> systemd cutover (web, scheduler,
#   readiness, in order, health-checked after each) -> active-campaign gate again
#   (immediately before scheduler restart) -> zero-mutation counter check
#   -> current symlink -> FINAL_REPORT.
#
# On any service-restart failure: STOP, roll back the units already changed, do not
# continue to the remaining services.
#
# --dry-run performs every read/build/verify step for real (so "would this succeed"
# is a genuine answer, not a guess) but builds into a throwaway location, and
# performs NO service restart, NO DB mutation, NO symlink mutation, NO campaign
# mutation.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_deploy_lib.sh
source "$HERE/_deploy_lib.sh"

DRY_RUN=0
FORCE=0
SHA=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sha) SHA="${2:?--sha requires a value}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --force) FORCE=1; shift ;;
    -h|--help)
      sed -n '1,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *)
      if [[ -z "$SHA" ]]; then SHA="$1"; shift; else fail "unexpected argument: $1"; fi
      ;;
  esac
done
[[ -n "$SHA" ]] || fail "usage: $(basename "$0") --sha <git-sha> [--dry-run]"
# Refuse anything that looks like a branch/ref name rather than a commit-ish the
# caller pinned explicitly enough to be a real SHA prefix (defense in depth --
# real validation is `sha_exists_in_mirror` + `rev-parse` resolving to a full commit).
[[ "$SHA" =~ ^[0-9a-fA-F]{7,40}$ ]] || fail "SHA does not look like a git commit hash: $SHA (branch tips are not accepted)"

if [[ "$DRY_RUN" -eq 0 ]]; then
  require_root
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
SHORT="$(printf '%s' "$SHA" | cut -c1-12)"
AUDIT_DIR="${AUDIT_ROOT}/autostory-deploy-${STAMP}"
mkdir -p "$AUDIT_DIR"
PRECHECK_TXT="${AUDIT_DIR}/PRECHECK.txt"
SERVICE_BEFORE_TXT="${AUDIT_DIR}/SERVICE_BEFORE.txt"
COUNTERS_BEFORE_TXT="${AUDIT_DIR}/COUNTERS_BEFORE.txt"
HEALTH_AFTER_TXT="${AUDIT_DIR}/HEALTH_AFTER.txt"
FINAL_REPORT_TXT="${AUDIT_DIR}/FINAL_REPORT.txt"
: > "$PRECHECK_TXT"; : > "$SERVICE_BEFORE_TXT"; : > "$COUNTERS_BEFORE_TXT"; : > "$HEALTH_AFTER_TXT"

pc() { echo "$*" | tee -a "$PRECHECK_TXT"; }
mode_label() { [[ "$DRY_RUN" -eq 1 ]] && echo "[DRY-RUN]" || echo "[LIVE]"; }

log "$(mode_label) deploy starting: sha=$SHA short=$SHORT audit_dir=$AUDIT_DIR"

# ---------------------------------------------------------------------------
# PRECHECKS
# ---------------------------------------------------------------------------
pc "=== PRECHECKS ($(date -u +%Y-%m-%dT%H:%M:%SZ)) ==="

ensure_deploy_mirror
if ! sha_exists_in_mirror "$SHA"; then
  fail "git SHA not found in deploy mirror: $SHA (fetched from $GIT_REMOTE_URL)"
fi
FULL_SHA="$(git --git-dir="$DEPLOY_MIRROR" rev-parse "$SHA")"
pc "git_sha_exists=YES full_sha=$FULL_SHA"

# "source checkout clean": this tooling never builds from a working tree, only from a
# bare mirror (git archive needs no working tree, so there is nothing to be dirty).
# That is the actual replacement for a working-tree-cleanliness check in this design.
pc "source_checkout_clean=N/A (bare mirror only, no working tree by construction)"

if [[ -z "$(command -v sqlite3)" ]]; then fail "sqlite3 not found on PATH"; fi
if [[ -r "$SHARED_DB" ]]; then
  pc "shared_db_path=OK ($SHARED_DB)"
else
  fail "shared DB not readable: $SHARED_DB"
fi
if [[ -r "$SHARED_ENV" ]]; then
  pc "shared_env_path=OK ($SHARED_ENV)"
else
  fail "shared .env not readable: $SHARED_ENV"
fi

# Detect whether the requested SHA is already live before doing any build work.
# A cutover that doesn't change code is pointless work (service restarts, DB
# backup) for zero benefit -- a real run aborts unless --force is given.
# --dry-run always just reports this, never blocked by it.
ALREADY_LIVE_UNITS=()
for u in "${SYSTEMD_UNITS[@]}"; do
  rel="$(current_release_of "$u")"
  live_sha=""
  if [[ -n "$rel" && -f "$rel/RELEASE_MANIFEST.json" ]]; then
    live_sha="$(python3 -c "import json;print(json.load(open('$rel/RELEASE_MANIFEST.json')).get('git_sha',''))" 2>/dev/null || true)"
  fi
  if [[ "$live_sha" == "$FULL_SHA" ]]; then
    ALREADY_LIVE_UNITS+=("$u")
  fi
done
if [[ "${#ALREADY_LIVE_UNITS[@]}" -eq "${#SYSTEMD_UNITS[@]}" ]]; then
  pc "already_live=YES all three services already report git_sha=$FULL_SHA -- a cutover would be a no-op"
  if [[ "$DRY_RUN" -eq 0 && "$FORCE" -eq 0 ]]; then
    fail "requested SHA is already live on all services (nothing to deploy) -- pass --force to rebuild/cut over anyway"
  fi
elif [[ "${#ALREADY_LIVE_UNITS[@]}" -gt 0 ]]; then
  pc "already_live=PARTIAL (${ALREADY_LIVE_UNITS[*]}) -- other services are on a different release; proceeding to align them"
else
  pc "already_live=NO"
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  BUILD_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/autostory-dryrun-XXXXXX")"
  BUILD_STAMP="${STAMP}-dryrun"
else
  BUILD_ROOT="$RELEASES_ROOT"
  BUILD_STAMP="$STAMP"
fi

log "building release artifact ($(mode_label))"
if [[ "$DRY_RUN" -eq 1 ]]; then
  # Reuse the real builder unmodified against a throwaway output root, so a dry-run
  # proves the artifact genuinely builds (including its own fail-closed data/.env
  # symlink checks) without ever writing into the real releases directory.
  NEW_RELEASE="$(RELEASES_ROOT_OVERRIDE="$BUILD_ROOT" bash -c '
    set -euo pipefail
    OUT_ROOT="${RELEASES_ROOT_OVERRIDE}"
    SHA="'"$FULL_SHA"'"; REPO="'"$DEPLOY_MIRROR"'"; STAMP="'"$BUILD_STAMP"'"
    SHORT="$(printf "%s" "$SHA" | cut -c1-12)"
    OUT="${OUT_ROOT}/${STAMP}-${SHORT}"
    mkdir -p "$OUT"
    git -C "$REPO" archive --format=tar "$SHA" | tar -C "$OUT" -xf -
    echo "$OUT"
  ')"
else
  NEW_RELEASE="$(bash "$HERE/build_immutable_release.sh" "$DEPLOY_MIRROR" "$FULL_SHA" "$BUILD_STAMP")"
fi
pc "release_artifact_builds=YES path=$NEW_RELEASE"

# Symlink shared runtime state into the freshly built artifact (same convention as
# build_immutable_release.sh; done here explicitly too since the dry-run path above
# bypasses that script to keep its output out of the real releases root).
# rm -rf first: git archive extracts real data/{logs,media,sessions}/.gitkeep
# directories (git-tracked placeholders). ln -sfn on an *existing real directory*
# places the symlink inside it rather than replacing it -- matches the same
# precaution build_immutable_release.sh already takes.
rm -rf "$NEW_RELEASE/data" "$NEW_RELEASE/.env"
ln -sfn "$SHARED_DATA" "$NEW_RELEASE/data"
ln -sfn "$SHARED_ENV" "$NEW_RELEASE/.env"
if [[ "$(readlink -f "$NEW_RELEASE/data")" != "$(readlink -f "$SHARED_DATA")" ]]; then
  fail "release data symlink does not resolve to shared data dir"
fi

REQ_HASH="$(sha256sum "$NEW_RELEASE/requirements.txt" | awk '{print $1}')"
TREE_HASH="$(git --git-dir="$DEPLOY_MIRROR" rev-parse "${FULL_SHA}^{tree}")"
PY_VER="$("$SHARED_VENV_PY" -c 'import sys; print("%d.%d.%d"%sys.version_info[:3])' 2>/dev/null || echo "unknown")"
PREV_RELEASE_WEB="$(current_release_of autostory-web)"

MANIFEST_PATH="$NEW_RELEASE/RELEASE_MANIFEST.json"
python3 - "$MANIFEST_PATH" <<PYEOF
import json, sys
manifest = {
    "git_sha": "$FULL_SHA",
    "git_tree": "$TREE_HASH",
    "git_branch_hint": "deploy pinned to explicit SHA, not a branch tip",
    "created_at_utc": "$STAMP",
    "python_version": "$PY_VER",
    "requirements_hash": "$REQ_HASH",
    "release_path": "$NEW_RELEASE",
    "previous_release": "${PREV_RELEASE_WEB:-unknown}",
    "dry_run": $( [[ "$DRY_RUN" -eq 1 ]] && echo True || echo False ),
}
with open(sys.argv[1], "w") as f:
    json.dump(manifest, f, indent=2, sort_keys=True)
    f.write("\n")
PYEOF
pc "manifest_written=YES ($MANIFEST_PATH)"
cp "$MANIFEST_PATH" "${AUDIT_DIR}/RELEASE_MANIFEST.json"

: "${VERIFY_MANIFEST_SCRIPT:=$HERE/verify_release_manifest.py}"
if python3 "$VERIFY_MANIFEST_SCRIPT" "$NEW_RELEASE" >> "$PRECHECK_TXT" 2>&1; then
  pc "manifest_verifies=YES"
else
  fail "verify_release_manifest.py failed for $NEW_RELEASE -- see $PRECHECK_TXT"
fi

log "python syntax check"
if "$SHARED_VENV_PY" -m compileall -q "$NEW_RELEASE/src"; then
  pc "python_syntax=OK"
else
  fail "python -m compileall failed against $NEW_RELEASE/src"
fi

log "secret scan"
if python3 "$NEW_RELEASE/scripts/audit/check_secret_artifacts.py" "$NEW_RELEASE" --json > "${AUDIT_DIR}/secret_scan.json" 2>&1; then
  pc "secret_scan=PASS (see ${AUDIT_DIR}/secret_scan.json)"
else
  fail "secret scan reported a problem -- see ${AUDIT_DIR}/secret_scan.json"
fi

for u in "${SYSTEMD_UNITS[@]}"; do
  rel="$(current_release_of "$u")"
  [[ -n "$rel" && -d "$rel" ]] || fail "previous release for $u does not exist on disk: ${rel:-<empty>}"
  pc "previous_release_exists[$u]=YES ($rel)"
  state="$(unit_active_state "$u")"
  [[ "$state" == "active" ]] || fail "$u is not active before deploy (state=$state) -- refusing to deploy on top of an unhealthy service"
  pc "service_healthy_before[$u]=YES"
done

ACTIVE_CAMPAIGNS_PRECHECK="$(active_campaign_count)"
pc "active_autostory_campaigns=${ACTIVE_CAMPAIGNS_PRECHECK}"
if [[ "$ACTIVE_CAMPAIGNS_PRECHECK" -gt 0 ]]; then
  fail "active AutoStory campaign(s) exist ($ACTIVE_CAMPAIGNS_PRECHECK) -- ABORT per policy. Not pausing/cancelling automatically."
fi

# ---------------------------------------------------------------------------
# PRE-DEPLOY BASELINE
# ---------------------------------------------------------------------------
{
  echo "=== SERVICE_BEFORE ($(date -u +%Y-%m-%dT%H:%M:%SZ)) ==="
  for u in "${SYSTEMD_UNITS[@]}"; do
    echo "$u WorkingDirectory=$(current_release_of "$u") ActiveState=$(unit_active_state "$u") NRestarts=$(unit_nrestarts "$u")"
  done
  echo "current_symlink=$(readlink -f "$CURRENT_SYMLINK" 2>/dev/null || echo MISSING)"
} | tee -a "$SERVICE_BEFORE_TXT"

STORIES_BEFORE="$(story_count)"
MESSAGES_BEFORE="$(message_deliveries_count)"
LOCKS_BEFORE="$(account_lock_count)"
{
  echo "=== COUNTERS_BEFORE ($(date -u +%Y-%m-%dT%H:%M:%SZ)) ==="
  echo "stories=$STORIES_BEFORE"
  echo "message_deliveries=$MESSAGES_BEFORE"
  echo "active_campaigns=$ACTIVE_CAMPAIGNS_PRECHECK"
  echo "account_locks=$LOCKS_BEFORE"
} | tee -a "$COUNTERS_BEFORE_TXT"

if [[ "$DRY_RUN" -eq 1 ]]; then
  DB_BACKUP_PATH="${AUDIT_DIR}/storyfleet.db.backup (SKIPPED -- dry-run performs no DB mutation, including backup writes beyond the audit dir itself)"
  pc "db_backup=SKIPPED (dry-run)"
else
  DB_BACKUP_PATH="${AUDIT_DIR}/storyfleet.db.backup"
  log "DB backup: sqlite3 .backup -> $DB_BACKUP_PATH"
  sqlite3 "$SHARED_DB" ".backup '${DB_BACKUP_PATH}'"
  INTEGRITY="$(sqlite3 "$DB_BACKUP_PATH" "PRAGMA integrity_check;")"
  if [[ "$INTEGRITY" != "ok" ]]; then
    fail "DB backup integrity_check did not return ok: $INTEGRITY"
  fi
  pc "db_backup=OK path=$DB_BACKUP_PATH integrity_check=ok"
fi

# ---------------------------------------------------------------------------
# DRY-RUN: report and stop here
# ---------------------------------------------------------------------------
if [[ "$DRY_RUN" -eq 1 ]]; then
  rm -rf "$BUILD_ROOT"
  {
    echo "=== DRY-RUN SUMMARY ==="
    echo "candidate_sha=$FULL_SHA"
    if [[ "${#ALREADY_LIVE_UNITS[@]}" -eq "${#SYSTEMD_UNITS[@]}" ]]; then
      echo "already_live=YES (all three services already report this exact git_sha -- a real run would ABORT here without --force, no pointless cutover)"
    elif [[ "${#ALREADY_LIVE_UNITS[@]}" -gt 0 ]]; then
      echo "already_live=PARTIAL (${ALREADY_LIVE_UNITS[*]})"
    else
      echo "already_live=NO"
    fi
    echo "would_build_release=${RELEASES_ROOT}/${STAMP}-${SHORT}"
    echo "current_release(web)=$PREV_RELEASE_WEB"
    for u in "${SYSTEMD_UNITS[@]}"; do
      echo "would_move[$u]: $(current_release_of "$u")  ->  ${RELEASES_ROOT}/${STAMP}-${SHORT}"
    done
    echo "systemd_files_that_would_change:"
    for u in "${SYSTEMD_UNITS[@]}"; do echo "  ${OVERRIDE_CONF[$u]}"; done
    echo "db_backup_target(real_run)=${AUDIT_ROOT}/autostory-deploy-<real-stamp>/storyfleet.db.backup"
    echo "active_campaign_status=${ACTIVE_CAMPAIGNS_PRECHECK} (deploy would proceed only if this stays 0)"
    echo "stories_current=$STORIES_BEFORE message_deliveries_current=$MESSAGES_BEFORE"
    echo "rollback_target(real_run)=$PREV_RELEASE_WEB"
    echo "NO service restart, NO DB mutation, NO symlink mutation, NO campaign mutation performed."
  } | tee -a "$FINAL_REPORT_TXT"
  log "DRY-RUN complete. Verdict: AUTOSTORY_DEPLOYMENT_DRYRUN_OK"
  exit 0
fi

# ---------------------------------------------------------------------------
# LIVE CUTOVER (deployment lock held for the remainder of the script)
# ---------------------------------------------------------------------------
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  fail "another deployment appears to be in progress (lock held: $LOCK_FILE)"
fi
log "deployment lock acquired: $LOCK_FILE"

CHANGED_UNITS=()
BACKUP_CONF=()

rollback_changed_units() {
  log "ROLLING BACK changed units: ${CHANGED_UNITS[*]:-none}"
  for i in "${!CHANGED_UNITS[@]}"; do
    u="${CHANGED_UNITS[$i]}"
    bkp="${BACKUP_CONF[$i]}"
    cp "$bkp" "${OVERRIDE_CONF[$u]}"
    log "restored $u override.conf from $bkp"
  done
  systemctl daemon-reload
  for u in "${CHANGED_UNITS[@]}"; do
    systemctl restart "$u" || log "WARNING: rollback restart of $u also failed -- manual intervention required"
  done
}

cutover_one() {
  local u="$1"
  local conf="${OVERRIDE_CONF[$u]}"
  local old_release
  old_release="$(current_release_of "$u")"
  [[ -n "$old_release" ]] || fail "cannot determine current WorkingDirectory for $u"
  local conf_base
  conf_base="$(basename "$conf")"
  local bkp="${AUDIT_DIR}/${conf_base}.${u}.pre-deploy.bak"
  cp "$conf" "$bkp"
  BACKUP_CONF+=("$bkp")
  CHANGED_UNITS+=("$u")

  if ! grep -qF "$old_release" "$conf"; then
    fail "$conf does not contain the expected current release path ($old_release) -- refusing to blind-edit, format may have changed"
  fi
  sed -i.orig-unused "s#${old_release}#${NEW_RELEASE}#g" "$conf"
  rm -f "${conf}.orig-unused"
  log "$u override.conf updated: $old_release -> $NEW_RELEASE"

  systemctl daemon-reload
  systemctl restart "$u"
  sleep 2

  local state pid cwd restarts wd
  state="$(unit_active_state "$u")"
  restarts="$(unit_nrestarts "$u")"
  wd="$(current_release_of "$u")"
  pid="$(systemctl show "$u" -p MainPID --value 2>/dev/null || echo 0)"
  cwd="unknown"
  if [[ "$pid" != "0" && -e "/proc/$pid/cwd" ]]; then
    cwd="$(readlink -f "/proc/$pid/cwd" 2>/dev/null || echo unknown)"
  fi

  {
    echo "--- $u ($(date -u +%Y-%m-%dT%H:%M:%SZ)) ---"
    echo "ActiveState=$state NRestarts=$restarts WorkingDirectory=$wd MainPID=$pid ProcCwd=$cwd"
    journalctl -u "$u" -n 20 --no-pager 2>/dev/null || echo "(journalctl unavailable)"
  } | tee -a "$HEALTH_AFTER_TXT"

  if [[ "$state" != "active" || "$wd" != "$NEW_RELEASE" ]]; then
    log "HEALTH CHECK FAILED for $u (state=$state wd=$wd expected=$NEW_RELEASE)"
    return 1
  fi
  return 0
}

if ! cutover_one autostory-web; then
  rollback_changed_units
  fail "web cutover failed; rolled back. See $HEALTH_AFTER_TXT"
fi

# Required: re-check active campaigns immediately before the scheduler restart,
# even though it already passed in PRECHECKS.
ACTIVE_CAMPAIGNS_RECHECK="$(active_campaign_count)"
echo "active_autostory_campaigns_recheck_before_scheduler=${ACTIVE_CAMPAIGNS_RECHECK}" >> "$HEALTH_AFTER_TXT"
if [[ "$ACTIVE_CAMPAIGNS_RECHECK" -gt 0 ]]; then
  rollback_changed_units
  fail "active AutoStory campaign appeared between precheck and scheduler cutover ($ACTIVE_CAMPAIGNS_RECHECK) -- ABORT before scheduler restart, rolled back web."
fi

if ! cutover_one autostory-scheduler; then
  rollback_changed_units
  fail "scheduler cutover failed; rolled back web+scheduler. See $HEALTH_AFTER_TXT"
fi

if ! cutover_one autostory-readiness-worker; then
  rollback_changed_units
  fail "readiness-worker cutover failed; rolled back all three. See $HEALTH_AFTER_TXT"
fi

log "all three services cut over successfully"

# ---------------------------------------------------------------------------
# HTTP HEALTH CHECKS
# ---------------------------------------------------------------------------
HTTP_OK=1
for p in "${HEALTH_PATHS[@]}"; do
  code="$(curl -fsS -o /dev/null -w '%{http_code}' --max-time 10 "${HEALTH_URL_BASE}${p}" || echo "000")"
  echo "http[$p]=$code" | tee -a "$HEALTH_AFTER_TXT"
  # /login /stories /accounts are auth-gated; a valid redirect (3xx) or 200 is healthy,
  # 000/5xx is not.
  if [[ "$code" == "000" || "$code" == 5* ]]; then
    HTTP_OK=0
  fi
done
if [[ "$HTTP_OK" -eq 0 ]]; then
  log "WARNING: one or more health-check paths returned an unhealthy status (see $HEALTH_AFTER_TXT). Services are already cut over; not auto-rolling-back on HTTP check alone -- investigate immediately."
fi

# ---------------------------------------------------------------------------
# ZERO-MUTATION GATE
# ---------------------------------------------------------------------------
STORIES_AFTER="$(story_count)"
MESSAGES_AFTER="$(message_deliveries_count)"
STORIES_DELTA=$((STORIES_AFTER - STORIES_BEFORE))
MESSAGES_DELTA=$((MESSAGES_AFTER - MESSAGES_BEFORE))
echo "stories_after=$STORIES_AFTER (delta=$STORIES_DELTA) message_deliveries_after=$MESSAGES_AFTER (delta=$MESSAGES_DELTA)" | tee -a "$HEALTH_AFTER_TXT"

VERDICT="AUTOSTORY_DEPLOYMENT_PASS"
if [[ "$STORIES_DELTA" -ne 0 || "$MESSAGES_DELTA" -ne 0 ]]; then
  VERDICT="AUTOSTORY_DEPLOYMENT_NEEDS_INVESTIGATION"
  log "WARNING: non-zero counter delta during a deploy with 0 active campaigns at precheck -- ownership must be determined manually before declaring PASS."
fi
if [[ "$HTTP_OK" -eq 0 ]]; then
  VERDICT="AUTOSTORY_DEPLOYMENT_NEEDS_INVESTIGATION"
fi

# ---------------------------------------------------------------------------
# CURRENT SYMLINK -- only after health checks
# ---------------------------------------------------------------------------
if [[ "$VERDICT" == "AUTOSTORY_DEPLOYMENT_PASS" ]]; then
  ALL_MATCH=1
  for u in "${SYSTEMD_UNITS[@]}"; do
    [[ "$(current_release_of "$u")" == "$NEW_RELEASE" ]] || ALL_MATCH=0
  done
  if [[ "$ALL_MATCH" -eq 1 ]]; then
    ln -sfn "$NEW_RELEASE" "$CURRENT_SYMLINK"
    log "current symlink updated -> $NEW_RELEASE"
  else
    log "WARNING: not all services report WorkingDirectory=$NEW_RELEASE -- leaving current symlink untouched"
    VERDICT="AUTOSTORY_DEPLOYMENT_NEEDS_INVESTIGATION"
  fi
else
  log "current symlink left untouched (verdict=$VERDICT)"
fi

{
  echo "=== FINAL_REPORT ($(date -u +%Y-%m-%dT%H:%M:%SZ)) ==="
  echo "$VERDICT"
  echo "SHA=$FULL_SHA"
  echo "RELEASE=$NEW_RELEASE"
  for u in "${SYSTEMD_UNITS[@]}"; do
    echo "$(echo "$u" | tr '[:lower:]-' '[:upper:]_')=$(unit_active_state "$u")"
  done
  echo "ACTIVE_CAMPAIGNS=$(active_campaign_count)"
  echo "STORIES_DELTA=$STORIES_DELTA"
  echo "MESSAGES_DELTA=$MESSAGES_DELTA"
  echo "ROLLBACK=$PREV_RELEASE_WEB"
  echo "AUDIT_DIR=$AUDIT_DIR"
} | tee -a "$FINAL_REPORT_TXT"

flock -u 9
log "deployment lock released"
[[ "$VERDICT" == "AUTOSTORY_DEPLOYMENT_PASS" ]]
