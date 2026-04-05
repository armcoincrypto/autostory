# STORYFLEET Healthcheck Phase 2 – Implementation Summary

**Status:** Implemented  
**Date:** 2025-03

## A. Root Cause (Why Phase 2)

- Sync `POST /api/accounts/healthcheck` was capped at 1–2 accounts to avoid Cloudflare 524.
- Operators could not reliably check all accounts from the dashboard.
- Phase 2 adds background jobs so long healthchecks run without blocking HTTP requests.

---

## B. File-by-File Patch Summary

### 1. `src/core/models.py`

Extended `HealthcheckRun`:

```python
# Background job fields (nullable for existing rows)
results = Column(JSON, nullable=True)      # List[dict] of per-account results
progress = Column(JSON, nullable=True)     # {"checked": N, "total": M}
error_message = Column(Text, nullable=True)
```

### 2. `src/core/database.py`

Added `_ensure_healthcheck_run_columns()` to create missing `results`, `progress`, `error_message` on `healthcheck_runs`, and call it from `init_db()`.

### 3. `src/clients/manager.py`

- Added optional `progress_callback(checked: int, total: int, result: dict)` to `check_accounts_health`.
- Invoked after each account result.
- Truncation (5-account cap) skipped when `progress_callback` is set (background job).

### 4. `src/dashboard/routes.py`

- **POST /api/accounts/healthcheck/start** – start background job, return `job_id` immediately.
- **GET /api/accounts/healthcheck/<int:job_id>** – return status, progress, results.
- `_run_healthcheck_background(run_id, account_ids, update_status, verbose)` – background thread that runs healthcheck and writes to DB.
- Throttling unchanged (60s cooldown).

### 5. `src/dashboard/templates/accounts.html`

- **Check selected (1–2)** – sync healthcheck for selected accounts (existing behavior).
- **Check all accounts** – starts background job, polls every 2s, shows progress and results.
- Shared modal and table layout for both flows.

---

## C. Verification Commands

```bash
# 1. Start background job
curl -i -s -X POST -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"update_status": false}' http://127.0.0.1:8000/api/accounts/healthcheck/start
# Expect: 201, {"success": true, "job_id": N}

# 2. Poll job status (replace 1 with job_id)
curl -s -H "X-Admin-Token: $TOKEN" http://127.0.0.1:8000/api/accounts/healthcheck/1 | jq .

# 3. Start with specific account_ids
curl -i -s -X POST -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"account_ids": [1,2,3], "update_status": false}' http://127.0.0.1:8000/api/accounts/healthcheck/start

# 4. Throttle: start again within 60s
curl -i -s -X POST -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{}' http://127.0.0.1:8000/api/accounts/healthcheck/start
# Expect: 429
```

---

## D. Rollback Notes

1. **Revert frontend**  
   - Remove "Check all accounts" button.  
   - Remove `checkAllAccounts()` and `healthcheckPollId`.  
   - Restore single "Check if accounts are alive" button.

2. **Revert routes**  
   - Remove `POST /accounts/healthcheck/start`.  
   - Remove `GET /accounts/healthcheck/<int:job_id>`.  
   - Remove `_run_healthcheck_background`.

3. **Schema**  
   - New columns (`results`, `progress`, `error_message`) are nullable; no downgrade needed for quick rollback.

4. **Manager**  
   - `progress_callback` is optional; revert only if behavior changes are not desired.

---

## E. API Contract

| Endpoint | Method | Auth | Purpose |
|----------|--------|------|---------|
| `/api/accounts/healthcheck` | POST | token/session | Sync check (1–2 accounts) |
| `/api/accounts/healthcheck/start` | POST | token/session | Start background job |
| `/api/accounts/healthcheck/<job_id>` | GET | token/session | Poll status |

**Start body:** `{ "account_ids": [1,2] | null, "update_status": bool, "verbose": bool }`  
**Poll response:** `{ "job_id", "status", "progress", "results", "started_at", "finished_at?", "error_message?" }`  
**Status values:** `running` | `success` | `error` | `timeout`
