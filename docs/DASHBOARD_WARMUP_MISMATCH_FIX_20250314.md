# Dashboard Warmup Mismatch Fix (2025-03-14)

## 1. Root Cause

**Exact reason dashboard was wrong after story precheck succeeded:**

`get_warmup_status()` in `src/core/warmup.py` returned DB `warmup_status` directly when it was one of `new`/`warming`/`warmed`/`risky`/`blocked`, without reconciling with `imported_at`. `warmup_status` is set to `"new"` or `"warming"` at import and is only updated to `"warmed"` when a story is successfully published (`src/stories/publisher.py`). Story precheck does **not** update `warmup_status`.

So an account could have:
- `story_precheck_status='allowed'`, `story_precheck_checked_at` = fresh
- `imported_at` = 5 days ago (past min_account_age_hours)
- `warmup_status` = `"warming"` (from import, never updated)

`is_warmup_blocked()` correctly used `first_seen + min_hours` and returned False (not blocked), so `get_story_safety_decision()` allowed the account. But `format_warmup_for_ui()` → `get_warmup_status()` returned `"warming"` from DB, so the dashboard showed a "Warming" badge while Story Status could show "Ready" and Story Available At "Now". The Warmup column was stale; summary counters could also be wrong because `warmup_pending` was derived from `warmup_status`/`warmup_block_reason`.

---

## 2. File-by-File Findings

### `src/core/warmup.py` — `get_warmup_status()`

- **Role:** Returns `new` | `warming` | `warmed` | `risky` | `blocked`.
- **Issue:** Lines 69–71 returned DB `warmup_status` immediately when it was set, without checking if the temporal gate had passed.
- **Used by:** `format_warmup_for_ui()`, `is_warmup_blocked()`, `get_account_risk_level()`.

### `src/core/warmup.py` — `format_warmup_for_ui()`

- **Role:** Returns `{warmup_status, warmup_label, is_blocked, warmup_block_reason}` for UI.
- **Source:** `get_warmup_status()` → labels: New, Warming, Warmed, Risky, Blocked.
- **Used by:** `list_accounts` (routes.py), `_accounts_summary()`.

### `src/dashboard/routes.py` — `list_accounts`

- **Row data:** Each account gets `warmup_status`, `warmup_label`, `warmup_block_reason` from `format_warmup_for_ui(a)`; `story_ui_status`, `story_available_label`, `story_reason`, `is_story_ready` from `get_story_availability(a)` → `get_story_safety_decision()`.
- **Summary:** `_accounts_summary_from_result()` uses `warmup_status`, `warmup_block_reason` for `warmup_pending`; `is_story_ready` for `story_available`; `story_ui_status` for `story_ok`, `story_frozen`, `story_rate_limited`.

### `src/dashboard/templates/accounts.html`

- **Warmup column:** `warmup_label` (Warmed / Warming / New / etc.).
- **Risk column:** `risk_level` from `get_account_risk_level()` → `get_warmup_status()`.
- **Story Status:** `story_ui_status` from `get_story_availability()`.
- **Story Precheck:** `story_precheck_status`, `story_precheck_stale`.
- **Story Available At:** `story_available_label` (Now / date / Precheck required / etc.).
- **Story Reason:** `story_reason` from `get_story_availability()`.

### `src/stories/precheck.py` — `persist_precheck_result()`

- **Updates:** `story_precheck_status`, `story_precheck_reason`, `story_precheck_checked_at`; when allowed: `story_status`, `story_status_reason`, `story_blocked_until`.
- **Does not update:** `warmup_status`.

---

## 3. Minimal Safe Fix

**File:** `src/core/warmup.py`  
**Change:** Reconcile `new`/`warming` with `imported_at`. When DB says `new` or `warming` but `first_seen + min_account_age_hours <= now`, return `warmed` so the display matches `is_warmup_blocked()` (which uses timestamps).

---

## 4. Verification Commands

```bash
cd /opt/autostory

# Syntax
python -m py_compile src/core/warmup.py

# Tests
python -m pytest tests/test_safety_policy.py -v --tb=short

# Restart web service (picks up warmup.py)
sudo systemctl restart autostory-web.service

# Confirm accounts 106–115 show Warmed when imported_at is old
sqlite3 /opt/autostory/data/storyfleet.db \
  "SELECT id, warmup_status, imported_at, story_precheck_status, story_precheck_checked_at FROM accounts WHERE id BETWEEN 106 AND 115;"

# API (with valid token)
curl -s -H "X-Admin-Token: $ADM" "http://127.0.0.1:8000/api/accounts?summary=1" | jq '.summary.story_available, .summary.warmup_pending'
curl -s -H "X-Admin-Token: $ADM" "http://127.0.0.1:8000/api/accounts?limit=200" | jq '.[] | select(.id >= 106 and .id <= 115) | {id, warmup_label, is_story_ready, story_ui_status}'
```

---

## 5. Rollback

```bash
cd /opt/autostory
git checkout HEAD -- src/core/warmup.py
sudo systemctl restart autostory-web.service
```
