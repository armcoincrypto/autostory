# TDATA Upload/Import Pipeline Audit

**Date:** 2025-03-14  
**Project:** STORYFLEET / autostory  
**Scope:** End-to-end audit of TDATA zip upload and account import flow  
**Purpose:** Verify production-readiness before operator uploads new accounts

---

## 1. Root Cause / Status Assessment

### Is TDATA upload currently production-ready?

**Yes, with caveats.** The pipeline is well-implemented with zip-slip protection, temp cleanup, per-account error handling, and canonical session persistence. The main risks are operator-related (wrong zip, overwriting existing accounts) rather than code bugs.

### What exactly works

| Component | Status |
|-----------|--------|
| Frontend upload (FormData, file + passcode) | OK |
| Auth (admin/session required via `require_admin_api`) | OK |
| Zip validation (50 MB max, .zip extension) | OK |
| Safe extraction (zip-slip, size/count limits) | OK |
| Multi-candidate discovery (inner zips, top-level dirs) | OK |
| TDATA → session conversion (opentele) | OK |
| Per-account import with `import_session_string` | OK |
| Canonical file write (`account_<id>.session`) | OK |
| DB updates (session_path, imported_at, import_source, warmup_status) | OK |
| Temp dir cleanup (`finally` block) | OK |
| Partial failure handling (continue on single failure) | OK |
| Session string deduplication in batch | OK |
| Scheduler provisioning after import | OK |
| Inspect-only endpoint (no import) | OK |
| Results modal with per-account status | OK |

### What is risky or needs operator care

| Risk | Severity | Mitigation |
|------|----------|------------|
| **Overwrite existing account** | Medium | Re-import of same phone/user_id **replaces** session. Uploading wrong tdata for an existing account will overwrite it. Operator must verify zip contents. |
| **session_file_saved=false** | Low | If `_save_string_session_to_canonical_file` fails (disk full, permissions), account row is created with `session_string` (raw string) but no canonical file. UI shows "Not saved". Account may work via StringSession but won't pass "has_session" (canonical file check). |
| **Long request timeout** | Low | Client uses 5 min timeout. For very large zips (many accounts), proxy/Cloudflare may time out before completion. |
| **Invalid/expired sessions in zip** | Expected | Some tdata folders may be revoked/expired. These fail with "Session not authorized" and are reported in results. Not a bug. |
| **Zip structure mismatch** | Expected | Zips from other tools (not Telegram Desktop tdata) may not have `map.json`. Use "Inspect zip" first to verify. |

### Exact operator risks before upload

1. **Verify zip contents:** Use "Inspect zip (no import)" before importing to confirm folder structure and candidate count.
2. **Avoid overwriting:** If re-importing to refresh sessions, ensure the zip contains the **correct** tdata for each account. Wrong tdata for same phone will overwrite.
3. **Check disk space:** Ensure `/opt/autostory/data/sessions/` has space for new `account_<id>.session` files.
4. **TELEGRAM_API_ID/HASH:** Must match the app that will use the sessions. Defaults to TelegramDesktop API if not set.

---

## 2. File-by-File Findings

### Frontend

| File | Role |
|------|------|
| `src/dashboard/templates/accounts.html` | Add Account → Import from tdata tab. Inputs: `tdata-zip` (file), `tdata-passcode`. Buttons: `importTdataZip()`, `inspectTdataZip()`. FormData: `file`, `passcode`. Fetch: POST `/api/accounts/import-tdata` or `/api/accounts/inspect-tdata`. 5 min timeout. Results modal with per-account status, session file saved/failed. |
| **Weak points** | None critical. Session string can be pasted in passcode field (no file) for single-account import. |

### Backend Routes

| File | Route | Role |
|------|-------|------|
| `src/dashboard/routes.py` | `POST /api/accounts/import-tdata` | Receives `request.files.get("file")`, `request.form.get("passcode")`. If passcode looks like session string (90+ chars, `1A...`), imports directly without file. Else: 50 MB limit, `.zip` required, `tempfile.mkdtemp(prefix="autostory_tdata_")`, save to `upload.zip`, `safe_extract_zip`, `discover_candidates`, loop over candidates calling `import_session_string`, `finally` `shutil.rmtree(tmpdir)`. |
| `src/dashboard/routes.py` | `POST /api/accounts/inspect-tdata` | Same validation; extract + discover only; no import. Returns candidate count, zip structure, failed_tdata. |
| **Auth** | `require_admin_api()` (before_request) applies to all `/api/*` except `/api/health`. Import routes are protected. |

### Import / Conversion Pipeline

| File | Role |
|------|------|
| `src/core/tdata_import.py` | `safe_extract_zip` (zip-slip, 120 MB, 10k files), `discover_candidates` (inner zips → account_0..N, top-level dirs, tdata by name, `find_all_sessions_fn` for .session/text). Dedup by session string. |
| `src/core/tdata_convert.py` | `tdata_to_session_string` (opentele TDesktop → Telethon), `find_tdata_root` (map.json search), `find_all_session_strings_in_extracted`, `session_file_to_string`. |
| `src/clients/manager.py` | `import_session_string`: connect, get_me, lookup by user_id then phone, create or update Account, `_save_string_session_to_canonical_file`, set session_path/session_string, imported_at, import_source, warmup_status. |
| **Canonical file** | `_save_string_session_to_canonical_file` → `get_canonical_session_path(account_id)` → `sessions_dir/account_<id>.session`. Uses SQLiteSession and persists auth_key. |
| `src/core/session_paths.py` | `get_sessions_dir()` from `settings.storage.sessions_dir` (default `./data/sessions`). `get_canonical_session_path(account_id)` = `sessions_dir/account_{id}.session`. |
| `config/settings.py` | `StorageSettings.sessions_dir` default `./data/sessions`. Env: `STORAGE_SESSIONS_DIR`. |

### Weak Points

- If `_save_string_session_to_canonical_file` fails, account gets `session_string = session.save()` (raw string) but no `session_path`. Healthcheck can still use StringSession if session_string is the string format, but `account_has_canonical_session` checks file existence → "Needs re-import" in UI. Result includes `session_file_saved: false`.
- No explicit rollback of partial DB writes if a later account in the batch fails. Each candidate is independent; already-imported accounts stay. Acceptable.

---

## 3. Exact Verification Commands

Run on server at `/opt/autostory`:

```bash
# 1. Route exists
grep -n "import-tdata\|import_tdata" src/dashboard/routes.py

# 2. Auth: import uses global admin gate (no per-route @admin_api_required needed; require_admin_api applies)
grep -n "require_admin_api\|def require_admin_api" src/dashboard/routes.py | head -5

# 3. Sessions dir
python3 -c "
from config.settings import settings
from pathlib import Path
p = Path(settings.storage.sessions_dir).expanduser().resolve()
print('Sessions dir:', p)
print('Exists:', p.is_dir())
if p.is_dir():
    print('Sample files:', list(p.glob('account_*.session'))[:5])
"

# 4. Temp dir prefix (for manual check if cleanup fails)
ls -la /tmp/autostory_tdata_* 2>/dev/null || echo "No stale temp dirs (good)"

# 5. DB: imported accounts
sqlite3 data/storyfleet.db "SELECT id, phone_number, import_source, imported_at, session_path FROM accounts WHERE import_source LIKE '%tdata%' OR import_source LIKE '%paste%' ORDER BY id DESC LIMIT 10;"

# 6. Canonical files for recent imports
for f in data/sessions/account_*.session; do
  [ -f "$f" ] && echo "$f: $(stat -c %s "$f" 2>/dev/null || stat -f %z "$f" 2>/dev/null) bytes"
done | tail -10

# 7. Test inspect (no import) with token
export ADM='YOUR_ADMIN_TOKEN'
curl -s -X POST -H "X-Admin-Token: $ADM" -F "file=@/path/to/test.zip" http://127.0.0.1:8000/api/accounts/inspect-tdata | jq .

# 8. Test import (will actually import - use a small test zip)
# curl -s -X POST -H "X-Admin-Token: $ADM" -F "file=@/path/to/real.zip" -F "passcode=" http://127.0.0.1:8000/api/accounts/import-tdata | jq .

# 9. Logs during upload
journalctl -u autostory-web.service -f &
# Then trigger import from UI; watch for import_tdata, import_session_string, session_saved_to_canonical_file
```

### Post-import healthcheck path

1. Import creates `Account` with `session_path` = canonical path.
2. `get_canonical_session_path(account_id)` returns `sessions_dir/account_{id}.session`.
3. Healthcheck (`check_accounts_health`) uses `get_canonical_session_path(a.id)`; if file exists, uses it as session. If not, falls back to `session_string`.
4. Healthcheck expects Telethon-compatible session (canonical SQLiteSession or StringSession).
5. Imported accounts appear in `/api/accounts` with `has_session` = True when canonical file exists.

---

## 4. If Changes Are Needed

**No minimal patch required for current audit.** The pipeline is production-ready. Optional hardening:

- **Optional:** Before overwriting existing account in `import_session_string`, log a warning when `existing` is found (helps audit "did I accidentally overwrite?").
- **Optional:** Add `STORAGE_SESSIONS_DIR` to `.env.example` so operators know it's configurable.
- **Optional:** If `session_file_saved` is False, consider retrying once or logging more detail (e.g. disk full, permission error). Current behavior is acceptable.

---

## 5. Rollback Notes

If an import causes issues:

1. **Remove account:** Use dashboard Delete or `DELETE /api/accounts/<id>`. This removes the DB row and canonical session file.
2. **Bulk delete:** `POST /api/accounts/bulk-delete` with `account_ids`.
3. **Restore from backup:** If DB was backed up before import, restore `accounts` table. Session files are separate; delete `account_<id>.session` for reverted accounts.
4. **No transaction rollback:** Import is not wrapped in a single transaction; each account commit is independent. To "undo" a batch, delete the imported account rows (and their session files) manually.

---

## 6. Final Operator Recommendation

### **Safe to upload new TDATA now**

**Conditions:**

1. Use **Inspect zip** first on any unfamiliar zip structure.
2. Ensure zip contains Telegram Desktop tdata (or .session files / session strings) in the expected layout (see `docs/TDATA_UPLOAD_PROBLEMS_AND_SPEC.md`).
3. For **re-import** (refreshing existing accounts), verify you're uploading the **correct** tdata for each account. Wrong tdata will overwrite.
4. Check disk space: `df -h /opt/autostory/data/`
5. Ensure `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` are set and match your app credentials.

### End-to-end flow (verified)

```
Select file → importTdataZip() → FormData(file, passcode)
  → POST /api/accounts/import-tdata (admin auth)
  → tempfile.mkdtemp, save upload.zip, safe_extract_zip
  → discover_candidates (inner zips + top-level dirs, tdata + find_all_sessions)
  → for each candidate: import_session_string(session_string, "tdata_zip")
       → connect, get_me, lookup user_id/phone
       → create or update Account
       → _save_string_session_to_canonical_file → data/sessions/account_<id>.session
       → set session_path, imported_at, import_source, warmup_status
  → finally: shutil.rmtree(tmpdir)
  → return { imported, updated, failed, results, session_file_failures }
  → UI: results modal, loadAccounts() on modal close
```

---

## Summary Table

| Check | Result |
|-------|--------|
| Zip-slip protection | Yes |
| Size limits | 50 MB zip, 120 MB extracted, 10k files |
| Temp cleanup | Yes (finally block) |
| Duplicate handling | Re-import updates existing (by user_id/phone) |
| Partial failure | Per-account; continues on error |
| Canonical file | `account_<id>.session` in sessions_dir |
| DB consistency | session_path, imported_at, import_source, warmup_status set |
| Overwrite risk | Yes – same phone/user_id gets replaced |
| Auth | Admin required |
