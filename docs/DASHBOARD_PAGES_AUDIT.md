# Dashboard Pages Reliability Audit

**Reference:** `/accounts` (known-good pattern)  
**Audited:** `/`, `/stories`, `/scheduler`, `/discovery`, `/campaigns`  
**Date:** 2025-03-14  
**Status:** Fixes applied. All smoke tests pass.

---

## 1. Audit Findings (Page by Page)

### / (Dashboard / index.html)

| Check | Status | Notes |
|-------|--------|-------|
| Redirect when unauthenticated | ✅ | `@login_required` |
| Loads when authenticated | ✅ | |
| /api/* return JSON, 403/500 safe | ⚠️ | No content-type check; `response.json()` throws on HTML |
| Stuck on "Loading..." | ⚠️ | **Recent Activity** card never updated—stays "Loading..." forever |
| Slow endpoints | ✅ | `/api/stats`, `/api/stories` are simple DB queries |
| Browser/parsing issues | ⚠️ | `fileInput?.files?.length` (optional chaining—IE11 fails) |

**Issues:**
1. `refreshStats()` / `loadRecentStories()`: no `credentials: 'same-origin'`, no content-type check, catch only `console.error`—stats stay "-" on 403/500.
2. **Recent Activity** card (`#recent-activity`) is never populated; always shows "Loading...".
3. `loadStoryAccounts()`: no credentials, silent failure.

---

### /stories (stories.html)

| Check | Status | Notes |
|-------|--------|-------|
| Redirect when unauthenticated | ✅ | `@login_required` |
| Loads when authenticated | ✅ | |
| /api/* return JSON, 403/500 safe | ⚠️ | No content-type check; many fetches lack it |
| Stuck on "Loading..." | ⚠️ | **loadStories** catch: only `console.error`—tbody stays "Loading..." |
| Slow endpoints | ✅ | `/api/stories`, `/api/accounts?summary=1` (latter fixed) |
| Browser/parsing issues | ⚠️ | `fileInput?.files?.length`, template literals |

**Issues:**
1. `loadStories()`: no credentials, no content-type check, no `response.ok` check; on error tbody never updated.
2. `loadAccounts()`, `loadStoryAccountBadges()`, `loadTemplates()`, `loadBlacklist()`, etc.: silent failures, no user feedback.
3. Pagination uses `onclick="loadStories(${i})"`—fine.
4. Many `?.` optional chaining usages.

---

### /scheduler (scheduler.html)

| Check | Status | Notes |
|-------|--------|-------|
| Redirect when unauthenticated | ✅ | `@login_required` |
| Loads when authenticated | ✅ | |
| /api/* return JSON, 403/500 safe | ✅ | `apiFetch()` checks `res.ok`, throws with message |
| Stuck on "Loading..." | ✅ | Uses toasts and explicit UI updates |
| Slow endpoints | ⚠️ | `/api/accounts/{id}/dialogs`—Telegram API call; can be slow |
| Browser/parsing issues | ⚠️ | `?.` optional chaining, `opts = {}` default param |

**Issues:**
1. `loadAccounts()`: empty `catch (_) {}`—fails silently; no error shown.
2. `/api/accounts/{id}/dialogs` may be slow (Telegram); not critical for initial load.
3. Otherwise scheduler has good error handling via `apiFetch` + `showToast`.

---

### /discovery (discovery.html)

| Check | Status | Notes |
|-------|--------|-------|
| Redirect when unauthenticated | ✅ | `@login_required` |
| Loads when authenticated | ✅ | |
| /api/* return JSON, 403/500 safe | ⚠️ | No content-type check |
| Stuck on "Loading..." | ⚠️ | **loadUsers** catch: only `console.error`—tbody stays "Loading..." |
| Slow endpoints | ⚠️ | `/api/discovery/scan` is slow (Telegram); stats/sources are fast |
| Browser/parsing issues | ⚠️ | Template literals |

**Issues:**
1. `loadStats()`, `loadSources()`, `loadUsers()`: no credentials, no content-type check.
2. `loadUsers()` catch: tbody never updated; stays "Loading...".
3. `loadSources()`: filter-source select not updated on error.
4. Chart.js may throw if `sourcesChart` not initialized—handled by destroy check.

---

### /campaigns (campaigns.html)

| Check | Status | Notes |
|-------|--------|-------|
| Redirect when unauthenticated | ✅ | `@login_required` |
| Loads when authenticated | ✅ | |
| /api/* return JSON, 403/500 safe | ⚠️ | No content-type check |
| Stuck on "Loading..." | ⚠️ | **loadCampaigns** catch: only `console.error`—grid stays "Loading campaigns..." |
| Slow endpoints | ✅ | `/api/campaigns` is simple |
| Browser/parsing issues | ⚠️ | Template literals, `onclick="toggleCampaign(${c.id}, ...)"` |

**Issues:**
1. `loadCampaigns()`: no credentials, no content-type check; on error grid never updated.
2. `toggleCampaign` / `deleteCampaign`: no `response.ok` check; silent fail on 403/500.

---

## 2. Priority Order

1. **Stories** — Most API calls, main data table stuck on Loading
2. **Campaigns** — Simple page, fully stuck on error
3. **Discovery** — Users table stuck on Loading
4. **Index** — Stats and Recent Activity issues
5. **Scheduler** — Minor: loadAccounts silent fail

---

## 3. Files to Change

| File | Changes |
|------|---------|
| `src/dashboard/templates/stories.html` | loadStories: content-type check, error row, credentials; loadAccounts: error fallback |
| `src/dashboard/templates/campaigns.html` | loadCampaigns: content-type check, error UI, credentials; toggle/delete: response.ok |
| `src/dashboard/templates/discovery.html` | loadUsers: content-type check, error row, credentials; loadStats/loadSources: error fallback |
| `src/dashboard/templates/index.html` | refreshStats, loadRecentStories, loadStoryAccounts: content-type + error UI; Recent Activity fallback |
| `src/dashboard/templates/scheduler.html` | loadAccounts: show toast on empty/fail (minimal) |

---

## 4. Reference Pattern (from /accounts)

```javascript
// credentials
var response = await fetch(url, { credentials: 'same-origin', ... });

// content-type check before .json()
var ct = (response.headers.get('content-type') || '');
if (ct.indexOf('application/json') === -1) {
    throw new Error('Server returned non-JSON. Check backend.');
}
var data = await response.json();

// response.ok
if (!response.ok) throw new Error((data && data.error) || response.statusText);

// catch: update DOM with error message, not just console.error
tbody.innerHTML = '<tr><td colspan="N">Failed: ' + esc(msg) + '</td></tr>';
```

---

## 5. Verification Commands

```bash
# Local (with session cookie from browser DevTools)
curl -i -s "http://127.0.0.1:8000/api/stats" -H "Cookie: session=YOUR_SESSION"
curl -i -s "http://127.0.0.1:8000/api/stories?per_page=5" -H "Cookie: session=YOUR_SESSION"
curl -i -s "http://127.0.0.1:8000/api/campaigns" -H "Cookie: session=YOUR_SESSION"
curl -i -s "http://127.0.0.1:8000/api/discovery/stats" -H "Cookie: session=YOUR_SESSION"
curl -i -s "http://127.0.0.1:8000/api/discovery/users?page=1&per_page=50" -H "Cookie: session=YOUR_SESSION"

# Unauthenticated (should 403 JSON)
curl -i -s "http://127.0.0.1:8000/api/stats"
# Expect: 403, JSON body
```

---

## 6. Tests to Add

```python
# tests/test_dashboard_pages.py
def test_unauthenticated_stories_redirects_to_login(client): ...
def test_authenticated_stories_returns_200(client, logged_in_client): ...
def test_unauthenticated_campaigns_redirects_to_login(client): ...
def test_authenticated_campaigns_returns_200(client, logged_in_client): ...
def test_unauthenticated_discovery_redirects_to_login(client): ...
def test_authenticated_discovery_returns_200(client, logged_in_client): ...
def test_api_stats_returns_json_when_authenticated(logged_in_client): ...
def test_api_campaigns_returns_json_when_authenticated(logged_in_client): ...
```
