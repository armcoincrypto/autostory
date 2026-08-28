#!/usr/bin/env bash
# Exercises deploy_production.sh --dry-run end-to-end against a throwaway local
# sandbox (fake systemd, fake DB, fake override.conf files, no sudo, no production
# contact). This is a development/CI aid, not part of the production tool chain --
# not referenced by deploy_production.sh, rollback_production.sh, or status.sh.
#
# Usage: bash scripts/release/local_sandbox_check.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"

SANDBOX="$(mktemp -d "${TMPDIR:-/tmp}/autostory-sandbox-XXXXXX")"
# Resolve to the physical path up front (macOS: /var -> /private/var) so every
# comparison against a resolved path downstream (readlink -f, Path.resolve()) stays
# consistent instead of intermittently mismatching on the symlink hop.
SANDBOX="$(cd "$SANDBOX" && pwd -P)"
trap 'rm -rf "$SANDBOX"' EXIT

echo "sandbox: $SANDBOX"

mkdir -p "$SANDBOX/bin" "$SANDBOX/releases/current-fake-release/src" "$SANDBOX/data" \
         "$SANDBOX/systemd.d/web" "$SANDBOX/systemd.d/scheduler" "$SANDBOX/systemd.d/readiness" \
         "$SANDBOX/audits"

FAKE_OLD_RELEASE="$SANDBOX/releases/20260101T000000Z-fakeold"
mkdir -p "$FAKE_OLD_RELEASE"

# --- fake shared runtime state (matches production shape: .env is a real file,
# confirmed via live `ls -la /opt/autostory`, not itself a symlink) ---
: > "$SANDBOX/.env"
mkdir -p "$SANDBOX/data/real"

python3 - "$SANDBOX/data/storyfleet.db" <<'PY'
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
db.executescript("""
CREATE TABLE stories (id INTEGER PRIMARY KEY);
CREATE TABLE message_deliveries (id INTEGER PRIMARY KEY);
CREATE TABLE auto_story_campaigns (id INTEGER PRIMARY KEY, status TEXT);
CREATE TABLE auto_story_account_locks (account_id INTEGER PRIMARY KEY);
INSERT INTO stories VALUES (1),(2),(3);
INSERT INTO message_deliveries VALUES (1),(2);
""")
db.commit()
PY

# --- fake override.conf files (must contain the "current" release path, like real ones) ---
for svc in web scheduler readiness; do
  cat > "$SANDBOX/systemd.d/$svc/override.conf" <<EOF
[Service]
WorkingDirectory=$FAKE_OLD_RELEASE
EOF
done

# --- fake systemctl / journalctl / curl on PATH, ahead of the real ones ---
cat > "$SANDBOX/bin/systemctl" <<EOF
#!/usr/bin/env bash
case "\$1" in
  show)
    unit="\$2"; shift 2
    conf=""
    case "\$unit" in
      autostory-web) conf="$SANDBOX/systemd.d/web/override.conf" ;;
      autostory-scheduler) conf="$SANDBOX/systemd.d/scheduler/override.conf" ;;
      autostory-readiness-worker) conf="$SANDBOX/systemd.d/readiness/override.conf" ;;
    esac
    while [[ \$# -gt 0 ]]; do
      case "\$1" in
        -p)
          prop="\$2"
          if [[ "\$prop" == "WorkingDirectory" ]]; then
            grep '^WorkingDirectory=' "\$conf" | head -1 | cut -d= -f2-
          elif [[ "\$prop" == "NRestarts" ]]; then
            echo 0
          elif [[ "\$prop" == "MainPID" ]]; then
            echo "\$\$"
          fi
          shift 2 ;;
        --value|--no-pager) shift ;;
        *) shift ;;
      esac
    done
    ;;
  is-active) echo active ;;
  daemon-reload) : ;;
  restart) : ;;
esac
EOF
chmod +x "$SANDBOX/bin/systemctl"

cat > "$SANDBOX/bin/journalctl" <<'EOF'
#!/usr/bin/env bash
echo "(sandbox: no real journal)"
EOF
chmod +x "$SANDBOX/bin/journalctl"

cat > "$SANDBOX/bin/curl" <<'EOF'
#!/usr/bin/env bash
# Only used for the -w '%{http_code}' health-check calls in this tool.
echo "200"
EOF
chmod +x "$SANDBOX/bin/curl"

cat > "$SANDBOX/bin/flock" <<'EOF'
#!/usr/bin/env bash
# Sandbox stub: -n <fd> just succeeds (dry-run never reaches flock, this is here
# only so a future --live sandbox test could exercise that path too).
exit 0
EOF
chmod +x "$SANDBOX/bin/flock"

# verify_release_manifest.py correctly hardcodes /opt/autostory/.env as the required
# .env symlink target -- that's right for real production and deliberately not made
# configurable there (it's a safety check, not a knob). For this portable sandbox we
# exercise the exact same verification logic against a patched *copy* instead of
# touching /opt on the dev machine or weakening the real script.
SANDBOX_VERIFY_SCRIPT="$SANDBOX/verify_release_manifest.sandboxed.py"
sed "s#/opt/autostory/\.env#$SANDBOX/.env#g" "$HERE/verify_release_manifest.py" > "$SANDBOX_VERIFY_SCRIPT"

VENV_PY="$REPO_ROOT/.venv312/bin/python"
[[ -x "$VENV_PY" ]] || VENV_PY="$(command -v python3)"

TEST_BASH="bash"
if (( BASH_VERSINFO[0] < 4 )); then
  for candidate in /opt/homebrew/bin/bash /usr/local/bin/bash; do
    [[ -x "$candidate" ]] && { TEST_BASH="$candidate"; break; }
  done
fi
echo "using bash: $TEST_BASH ($("$TEST_BASH" --version | head -1))"

SHA="$(git -C "$REPO_ROOT" rev-parse HEAD)"

echo "== running deploy_production.sh --dry-run against sandbox (sha=$SHA) =="
PATH="$SANDBOX/bin:$PATH" \
RELEASES_ROOT="$SANDBOX/releases" \
SHARED_ENV="$SANDBOX/.env" \
SHARED_DATA="$SANDBOX/data/real" \
SHARED_DB="$SANDBOX/data/storyfleet.db" \
SHARED_VENV_PY="$VENV_PY" \
DEPLOY_MIRROR="$SANDBOX/mirror.git" \
GIT_REMOTE_URL="$REPO_ROOT" \
OVERRIDE_CONF_WEB="$SANDBOX/systemd.d/web/override.conf" \
OVERRIDE_CONF_SCHEDULER="$SANDBOX/systemd.d/scheduler/override.conf" \
OVERRIDE_CONF_READINESS="$SANDBOX/systemd.d/readiness/override.conf" \
AUDIT_ROOT="$SANDBOX/audits" \
LOCK_FILE="$SANDBOX/deploy.lock" \
VERIFY_MANIFEST_SCRIPT="$SANDBOX_VERIFY_SCRIPT" \
  "$TEST_BASH" "$HERE/deploy_production.sh" --sha "$SHA" --dry-run
DEPLOY_EXIT=$?

echo
echo "== sandbox exit code: $DEPLOY_EXIT =="
echo "== audit dir contents =="
find "$SANDBOX/audits" -type f | sort
echo
echo "== FINAL_REPORT (dry-run summary) =="
cat "$SANDBOX"/audits/*/FINAL_REPORT.txt

# --- second scenario: requested SHA already live on all three services ---
# Seed the "current" release with a RELEASE_MANIFEST.json matching $SHA, then
# confirm dry-run reports already_live=YES and still exits 0 (never blocked).
echo
echo "== scenario 2: SHA already live (dry-run must report it, never block) =="
python3 - "$FAKE_OLD_RELEASE/RELEASE_MANIFEST.json" "$SHA" <<'PY'
import json, sys
json.dump({"git_sha": sys.argv[2]}, open(sys.argv[1], "w"))
PY

PATH="$SANDBOX/bin:$PATH" \
RELEASES_ROOT="$SANDBOX/releases" \
SHARED_ENV="$SANDBOX/.env" \
SHARED_DATA="$SANDBOX/data/real" \
SHARED_DB="$SANDBOX/data/storyfleet.db" \
SHARED_VENV_PY="$VENV_PY" \
DEPLOY_MIRROR="$SANDBOX/mirror.git" \
GIT_REMOTE_URL="$REPO_ROOT" \
OVERRIDE_CONF_WEB="$SANDBOX/systemd.d/web/override.conf" \
OVERRIDE_CONF_SCHEDULER="$SANDBOX/systemd.d/scheduler/override.conf" \
OVERRIDE_CONF_READINESS="$SANDBOX/systemd.d/readiness/override.conf" \
AUDIT_ROOT="$SANDBOX/audits" \
LOCK_FILE="$SANDBOX/deploy.lock" \
VERIFY_MANIFEST_SCRIPT="$SANDBOX_VERIFY_SCRIPT" \
  "$TEST_BASH" "$HERE/deploy_production.sh" --sha "$SHA" --dry-run > "$SANDBOX/scenario2.out" 2>&1
SCENARIO2_EXIT=$?
cat "$SANDBOX/scenario2.out"
if [[ "$SCENARIO2_EXIT" -ne 0 ]]; then
  echo "FAIL: dry-run must exit 0 even when the SHA is already live"
  exit 1
fi
if ! grep -q "already_live=YES" "$SANDBOX/scenario2.out"; then
  echo "FAIL: dry-run did not report already_live=YES for a SHA that is already live on all three services"
  exit 1
fi
echo "scenario 2: PASS (already_live=YES reported, dry-run still exited 0)"

echo
echo "LOCAL_SANDBOX_CHECK=PASS"
