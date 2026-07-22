#!/usr/bin/env bash
set -euo pipefail
REPO="${1:?repo path}"
SHA="${2:?git sha}"
STAMP="${3:-$(date -u +%Y%m%dT%H%M%SZ)}"
SHORT="$(printf '%s' "$SHA" | cut -c1-12)"
OUT="/opt/autostory-releases/${STAMP}-${SHORT}"
if [[ -e "$OUT" ]]; then
  echo "release path exists: $OUT" >&2
  exit 1
fi
mkdir -p "$OUT"
git -C "$REPO" archive --format=tar "$SHA" | tar -C "$OUT" -xf -
# Remove prohibited if archive ever included them (should not)
rm -rf "$OUT/.git" "$OUT/.env" "$OUT/data" "$OUT/venv" "$OUT/.venv" 2>/dev/null || true
# External shared refs (same convention as 499758e release)
ln -sfn /opt/autostory/data "$OUT/data"
ln -sfn /opt/autostory/.env "$OUT/.env"
# Fail closed: never ship a release with a local SQLite DB under data/
if [[ ! -L "$OUT/data" ]]; then
  echo "release data must be symlink to /opt/autostory/data: $OUT/data" >&2
  exit 1
fi
data_target="$(readlink -f "$OUT/data")"
if [[ "$data_target" != "/opt/autostory/data" ]]; then
  echo "release data symlink target mismatch: $data_target (expected /opt/autostory/data)" >&2
  exit 1
fi
if [[ -e "$OUT/data/storyfleet.db" ]]; then
  db_real="$(readlink -f "$OUT/data/storyfleet.db")"
  if [[ "$db_real" != "/opt/autostory/data/storyfleet.db" ]]; then
    echo "release resolves storyfleet.db away from shared DB: $db_real" >&2
    exit 1
  fi
fi
REQ_HASH="$(sha256sum "$OUT/requirements.txt" | awk '{print $1}')"
TREE_HASH="$(git -C "$REPO" rev-parse "${SHA}^{tree}")"
PY_VER="$(/opt/autostory/venv/bin/python -c 'import sys; print("%d.%d.%d"%sys.version_info[:3])')"
NODE_VER="$(node --version 2>/dev/null || echo n/a)"
python3 - <<PY
import json, pathlib
manifest = {
  "release_id": "${STAMP}-${SHORT}",
  "git_sha": "$SHA",
  "branch": "$(git -C "$REPO" rev-parse --abbrev-ref HEAD)",
  "build_timestamp_utc": "$STAMP",
  "source_tree_hash": "$TREE_HASH",
  "dependency_lock_hashes": {"requirements.txt": "$REQ_HASH"},
  "python_version": "$PY_VER",
  "node_version": "$NODE_VER",
  "database_schema_version": "sqlite-shared-external",
  "scheduler_entrypoint": "main.py scheduler",
  "readiness_entrypoint": "scripts/run_readiness_worker.py",
  "web_entrypoint": "wsgi:app",
  "environment_fingerprint_redacted": {
    "env_reference": "/opt/autostory/.env",
    "venv_reference": "/opt/autostory/venv",
    "data_reference": "/opt/autostory/data",
  },
  "mutation_lock_expected": {
    "SCHEDULER_MUTATIONS_ENABLED": "false",
    "STORY_EXECUTION_ENABLED": "false",
    "CAMPAIGN_EXECUTION_ENABLED": "false",
    "DISCOVERY_EXECUTION_ENABLED": "false",
  },
  "telegram_gateway_expected": "stopped",
  "telegram_bot_expected": "stopped",
  "kathleen_expected": "stopped",
  "ai_autopublish_expected": "disabled_no_worker",
  "rollback_release": "/opt/autostory-releases/20260721T232351Z-499758e3465a",
  "method": "git archive",
  "source_repository": "$REPO",
  "release_path": "$OUT",
  "notes": "Clean tracked source only; production data/secrets/venv referenced externally.",
  "data_link": "symlink:/opt/autostory/data",
  "fixed_nested_data_symlink": True,
}
path = pathlib.Path("$OUT/RELEASE_MANIFEST.json")
path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
print(path)
PY
echo "$OUT"
