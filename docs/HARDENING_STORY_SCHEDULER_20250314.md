# Story Batch & Scheduler Hardening (2025-03-14)

## 1. Root Cause Summary

### Why Story Batch Allowed Avoidable Failures

- **No pre-submit eligibility check**: The Publish button was always enabled. Operators could upload media and click Publish even when zero accounts were story-eligible (warming, precheck missing, no session, etc.). The request was sent; the backend returned "No eligible accounts" after the fact.
- **No manual-selection validation**: In manual mode, the operator could submit with no accounts selected, or select accounts that became ineligible. The UI did not validate before submit.
- **Raw exception display**: Backend errors (e.g. `session_db_locked`, trace snippets) were shown directly in alerts.
- **Banner/button state**: The modal never reflected "no eligible" state; the Publish button was never disabled when a run would guaranteed fail.

### Why Scheduler Allowed Broken Runs

- **No account session pre-check**: The run-now API created jobs for accounts without canonical session files. `get_fresh_client` failed with NoClient; the error surfaced as raw "Failed to get client" or similar.
- **Invalid targets treated as usable**: Targets without `tg_id`, `username`, or `invite_link` were displayed as normal options. Sending to them always failed with UsernameInvalidError or "Could not find entity."
- **No pre-validation before run**: Save and Send now did not validate account session, target validity, or non-empty message before submitting.
- **Raw error messages**: Telethon/Python exceptions (e.g. `UsernameInvalidError`, "Cannot find any entity") were returned to the UI as-is.

---

## 2. File-by-File Findings and Patches

### `src/dashboard/templates/stories.html`

| Weak point | Patch |
|------------|-------|
| Publish always enabled | Fetch `/api/stories/eligible-accounts` when modal opens; disable Publish when `batchEligibleCount === 0` |
| No eligibility banner | Added `#batch-eligibility-banner` with reasons when zero eligible |
| No manual selection validation | Pre-submit: require at least one account when manual mode |
| Raw errors in alerts | `normalizeBatchError()` maps known patterns to operator messages |
| batchEligibleCount not tracked | Added `batchEligibleCount`, `batchExclusionReasons`; `updateBatchEligibility()`, `updateBatchEligibilityUI()` |

### `src/dashboard/templates/scheduler.html`

| Weak point | Patch |
|------------|-------|
| Accounts without session shown | Filter dropdown to `has_session === true`; fallback message when none have session |
| Invalid targets selectable | `isValidTarget(t)` = tg_id OR username OR invite_link; mark invalid with badge; `getValidSelectedTargetIds()` filters at submit |
| No pre-validation on Save/Send | Check account `has_session === true`, ≥1 valid target, non-empty body before submit |
| Raw errors from run-now | `normalizeSchedulerError()` for toast messages; backend normalizes before response |
| Summary used all checked targets | `updateSimpleSummary()` uses `getValidSelectedTargetIds()` |

### `src/dashboard/scheduler_routes.py`

| Weak point | Patch |
|------------|-------|
| Run-now no pre-check | Before creating job: verify `account_has_canonical_session(account)`, target has tg_id/username/invite_link |
| Raw error returned to UI | `_normalize_scheduler_error(err_code, err_msg)` maps known codes to operator-friendly strings |
| No target validity check | Validate target has `tg_id` or `username` or `invite_link` before run |

### `src/scheduler/executor.py`

| Weak point | Patch |
|------------|-------|
| UsernameInvalidError / UsernameNotOccupiedError surfaced raw | Added to `_map_error()`; maps to `InvalidUsername` / `UsernameNotOccupied` with clean message |
| Fallback when Telethon lacks these | Check `type(exc).__name__` for "Username" + "Invalid"/"Occupied" |

---

## 3. Verification Commands

```bash
cd /opt/autostory

# Syntax check
python -m py_compile src/dashboard/scheduler_routes.py src/scheduler/executor.py

# Restart web service (picks up template + route changes)
sudo systemctl restart autostory-web.service

# Restart scheduler if executor changed (in-process; reloads on next run)
sudo systemctl restart autostory-scheduler.service

# Check services
systemctl --no-pager status autostory-web.service autostory-scheduler.service

# Logs while testing
journalctl -u autostory-web.service -f
```

### Route checks (with valid X-Admin-Token)

```bash
export ADM='YOUR_ADMIN_TOKEN'

# Story eligible-accounts (used by batch modal)
curl -s -H "X-Admin-Token: $ADM" http://127.0.0.1:8000/api/stories/eligible-accounts | jq '.accounts | length, .exclusion_reasons'

# Scheduler run-now pre-check (400 when no session)
curl -s -X POST -H "X-Admin-Token: $ADM" -H "Content-Type: application/json" \
  -d '{"account_id":999,"target_id":1,"type":"PROMO"}' \
  http://127.0.0.1:8000/api/v1/jobs/run-now | jq .
```

### DB / session checks

```bash
# Accounts with session
sqlite3 /opt/autostory/data/storyfleet.db "SELECT id, phone_number, purpose FROM accounts LIMIT 5"

# Canonical session files
ls -la /opt/autostory/data/sessions/account_*.session 2>/dev/null | wc -l
```

---

## 4. Manual Test Plan

### Batch Publish Stories

1. **Zero eligible**
   - Ensure all accounts are warming, no precheck, or no session.
   - Open Batch Publish modal.
   - Expect: banner "No story-eligible accounts right now" with reasons; Publish disabled.
   - Try Preview: should show 0 eligible.
   - Try Publish: already disabled; if enabled by race, pre-submit alert blocks.

2. **Manual selection invalid**
   - Switch to "Choose accounts manually".
   - Do not select any account.
   - Select media, click Publish.
   - Expect: alert "Select at least one story-eligible account."

3. **Media required**
   - Leave media unselected.
   - Click Publish.
   - Expect: alert "Please select a photo or video file." (unchanged)

4. **At least one eligible**
   - Have ≥1 story-eligible account.
   - Open modal: no banner, Publish enabled.
   - Run preview, then publish with media.
   - Expect: batch runs or shows normalized error (no raw trace).

### Scheduler

1. **No valid account session**
   - Use account with no canonical session file (or delete session for one).
   - Load Scheduler: if no accounts have session, expect message "No messaging accounts have a valid session. Re-add or re-import in Accounts."
   - If at least one has session, select one without; try Save or Send now.
   - Expect: toast "Account has no valid session. Re-add or re-import in Accounts."

2. **Invalid target**
   - Add target with no tg_id, username, or invite_link (or edit existing).
   - Select it, try Save or Send now.
   - Expect: targets marked "Invalid" badge; selected invalid ones excluded; if all invalid, toast "Select at least one valid target."

3. **Empty message**
   - Clear message text.
   - Click Save or Send now.
   - Expect: toast "Enter message text." (unchanged)

4. **Valid flow**
   - Account with session, ≥1 valid target, non-empty message.
   - Save: success.
   - Send now: either success or normalized error (e.g. "Invalid target username. Use invite link (t.me/+xxx) for private groups.") — no raw UsernameInvalidError.

---

## 5. Rollback Notes

To revert all changes:

```bash
cd /opt/autostory

# Restore files from git (if committed before)
git checkout HEAD -- \
  src/dashboard/templates/stories.html \
  src/dashboard/templates/scheduler.html \
  src/dashboard/scheduler_routes.py \
  src/scheduler/executor.py

# Restart services
sudo systemctl restart autostory-web.service
sudo systemctl restart autostory-scheduler.service
```

If you have uncommitted changes, back up first:

```bash
cp src/dashboard/templates/stories.html src/dashboard/templates/stories.html.hardening
cp src/dashboard/templates/scheduler.html src/dashboard/templates/scheduler.html.hardening
# ... etc
```

---

## 6. Summary of Changes

| Area | Change |
|------|--------|
| Stories | Eligibility fetched on modal open; Publish disabled when 0 eligible; banner with reasons |
| Stories | Manual mode: require ≥1 account selected before submit |
| Stories | `normalizeBatchError()` for alerts; no raw exceptions |
| Scheduler | Account dropdown filters to `has_session === true`; message when none |
| Scheduler | Invalid targets marked and excluded from send payload; block when 0 valid |
| Scheduler | Pre-validate account session, ≥1 valid target, non-empty body before Save/Send |
| Scheduler API | Pre-check account session and target validity before creating job |
| Scheduler API | `_normalize_scheduler_error()` for run-now responses |
| Executor | Map UsernameInvalidError, UsernameNotOccupiedError to clean codes/messages |
