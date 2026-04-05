# Dashboard Functional Audit — Endpoint Inventory

**Date:** 2025-03-14  
**Goal:** Map all API endpoints, identify HTML/524 causes, fix root cause

---

## 1. Endpoint Inventory Table

| Page | Frontend function | Endpoint | Auth | Status | Latency | Risk |
|------|-------------------|----------|------|--------|---------|------|
| / | refreshStats | GET /api/stats | session | OK | Fast | Low |
| / | loadRecentStories | GET /api/stories?per_page=5 | session | OK | Fast | Low |
| / | loadStoryAccounts | GET /api/accounts | session | OK | Fast (optimized) | Low |
| / | startAuth, completeAuth, importSession | POST /api/accounts/auth/* | session | OK | Variable | Low |
| / | publishStory | POST /api/stories/publish | session | OK | Variable | Low |
| / | scanChannel | POST /api/discovery/scan | session | OK | Slow (Telegram) | Med |
| **/accounts** | loadAccounts | GET /api/accounts?summary=1 | session | OK | Fast (optimized) | Low |
| **/accounts** | **checkAccounts** | **POST /api/accounts/healthcheck** | session | **FIXED** | Was slow | **Was 524** |
| /accounts | session audit | GET /api/accounts/session-audit | session | OK | Moderate | Low |
| /accounts | Various | POST /api/accounts/* | session | OK | Variable | Low |
| /stories | loadStories | GET /api/stories | session | OK | Fast | Low |
| /stories | loadAccounts, loadStoryAccountBadges | GET /api/accounts, ?summary=1 | session | OK | Fast | Low |
| /stories | Various publish/batch | POST /api/stories/* | session | OK | Variable | Low |
| /discovery | loadStats, loadSources, loadUsers | GET /api/discovery/* | session | OK | Fast | Low |
| /discovery | startScan | POST /api/discovery/scan | session | OK | Slow | Med |
| /campaigns | loadCampaigns | GET /api/campaigns | session | OK | Fast | Low |
| /scheduler | apiFetch | /api/accounts, /api/v1/* | session/token | OK | Variable | Low |
| /scheduler | loadMyGroupsClick | GET /api/accounts/{id}/dialogs | session | OK | Slow (Telegram) | Med |

---

## 2. Root Cause of "Check failed: Server returned an HTML page instead of JSON (HTTP 524)"

**Endpoint:** POST /api/accounts/healthcheck  
**Frontend:** checkAccounts() in accounts.html

**Cause:**
- Healthcheck runs Telegram API calls (connect, auth, get_me) for **all accounts** when account_ids not provided
- Concurrency = 1 (sequential)
- ~25s per account × N accounts → easily >100s with 5+ accounts
- **Cloudflare origin timeout ≈ 100s** → 524 when backend takes longer
- 524 response is HTML → frontend JSON parse fails → shows "Server returned an HTML page instead of JSON"

**Fix applied:**
1. **Backend:** When account_ids not provided, limit to first 5 accounts with sessions
2. **Response:** Add `truncated: true` and `total_eligible: N` when limited
3. **Frontend:** Show "Checked first 5 of N accounts. Run again to check more." when truncated

---

## 3. Files Changed

| File | Change |
|------|--------|
| src/clients/manager.py | Limit account_list to 5 when account_ids is None; populate out_meta |
| src/dashboard/routes.py | Pass out_meta to healthcheck; merge into JSON response |
| src/dashboard/templates/accounts.html | Show truncation note when data.truncated |

---

## 4. Diffs (Summary)

### manager.py
- Added `out_meta: Optional[dict] = None` param
- After building account_list: if account_ids is None and len > 5, slice to [:5], set out_meta["truncated"] and ["total_eligible"]

### routes.py
- `out_meta = {}` before call
- Pass `out_meta=out_meta` to check_accounts_health
- Return `**out_meta` in jsonify

### accounts.html
- When data.truncated && data.total_eligible: insert note "Checked first 5 of N accounts. Run again to check more."

---

## 5. Verification Commands

```bash
# Healthcheck with session (local)
curl -i -s -X POST http://127.0.0.1:8000/api/accounts/healthcheck \
  -H "Content-Type: application/json" \
  -H "Cookie: session=YOUR_SESSION" \
  -d '{"update_status": false}'

# Expect: 200, application/json, {"success": true, "results": [...], "run_id": N}
# If >5 accounts with sessions: also "truncated": true, "total_eligible": N

# Timing (should complete <100s with fix)
time curl -s -o /dev/null -X POST http://127.0.0.1:8000/api/accounts/healthcheck \
  -H "Content-Type: application/json" \
  -H "Cookie: session=YOUR_SESSION" \
  -d '{"update_status": false}'

# Public (with token)
curl -i -s -X POST https://zellotex.com/api/accounts/healthcheck \
  -H "Content-Type: application/json" \
  -H "X-Admin-Token: YOUR_TOKEN" \
  -d '{"update_status": false}'
```

---

## 6. Other Potentially Slow Endpoints (Not 524 Source)

| Endpoint | Reason | Mitigation |
|----------|--------|------------|
| GET /api/accounts/{id}/dialogs | Telegram API | User-triggered; optional |
| POST /api/discovery/scan | Telegram API | User-triggered; shows progress |
| POST /api/accounts/story-precheck | Telegram API | Throttled; user selects accounts |
| POST /api/stories/batch | Telegram API | User-triggered |

---

## 7. Production Safety

- **Conservative:** Only limits healthcheck when no account_ids specified
- **Backward compatible:** When account_ids provided, no limit applied
- **Auth unchanged:** Session and token auth preserved
- **No frontend hacks:** Fix is backend; frontend only shows truncation message

---

## 8. Rollback

```bash
git checkout HEAD -- src/clients/manager.py src/dashboard/routes.py src/dashboard/templates/accounts.html
```
