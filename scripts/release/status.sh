#!/usr/bin/env bash
# Read-only AutoStory production status. No writes, no restarts, no locks taken.
# Usage: sudo scripts/release/status.sh   (root needed only to read systemd unit properties
# reliably across all setups; the checks themselves never mutate anything)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_deploy_lib.sh
source "$HERE/_deploy_lib.sh"

echo "== AutoStory production status ($(date -u +%Y-%m-%dT%H:%M:%SZ)) =="
echo

echo "-- current symlink --"
if [[ -L "$CURRENT_SYMLINK" ]]; then
  readlink -f "$CURRENT_SYMLINK"
else
  echo "MISSING or not a symlink: $CURRENT_SYMLINK"
fi
echo

echo "-- service releases / state --"
for u in "${SYSTEMD_UNITS[@]}"; do
  rel="$(current_release_of "$u")"
  state="$(unit_active_state "$u")"
  restarts="$(unit_nrestarts "$u")"
  printf '%-28s state=%-10s NRestarts=%-4s WorkingDirectory=%s\n' "$u" "$state" "$restarts" "${rel:-unknown}"
done
echo

echo "-- git identity of each running release (RELEASE_MANIFEST.json, if present) --"
for u in "${SYSTEMD_UNITS[@]}"; do
  rel="$(current_release_of "$u")"
  if [[ -n "$rel" && -f "$rel/RELEASE_MANIFEST.json" ]]; then
    sha="$(python3 -c "import json;print(json.load(open('$rel/RELEASE_MANIFEST.json')).get('git_sha','?'))" 2>/dev/null || echo "?")"
    echo "$u -> git_sha=$sha"
  else
    echo "$u -> RELEASE_MANIFEST.json not found at $rel"
  fi
done
echo

echo "-- database (read-only) --"
if [[ -r "$SHARED_DB" ]]; then
  echo "stories total:            $(story_count)"
  echo "message_deliveries total: $(message_deliveries_count)"
  echo "active AutoStory campaigns: $(active_campaign_count)"
  echo "account locks:             $(account_lock_count)"
else
  echo "DB not readable: $SHARED_DB"
fi
echo

echo "-- releases on disk (newest last) --"
for entry in "$RELEASES_ROOT"/*/; do
  name="$(basename "$entry")"
  [[ "$name" == "current" ]] && continue
  echo "$name"
done 2>/dev/null || true
echo

echo "-- deploy lock --"
if [[ -e "$LOCK_FILE" ]]; then
  echo "lock file present: $LOCK_FILE (a deploy may be in progress -- this alone does not prove it; flock is advisory)"
else
  echo "no lock file present"
fi
