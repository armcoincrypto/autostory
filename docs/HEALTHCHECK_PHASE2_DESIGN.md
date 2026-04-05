# STORYFLEET Healthcheck Phase 2: Background-Job Design

**Status:** Design (not implemented)  
**Goal:** Replace synchronous healthcheck with a job-based flow so no request blocks long enough to hit Cloudflare 524.

---

## 1. Architecture

### Current (Phase 1)
- Frontend `POST /api/accounts/healthcheck` → blocks until done (or 524)
- Backend runs `check_accounts_health` in-process with truncation (max 5 when no account_ids)
- Returns JSON with results immediately (or timeout)

### Target (Phase 2)
```
┌─────────────┐    POST /api/accounts/healthcheck/start     ┌──────────────┐
│  Frontend   │ ──────────────────────────────────────────>│   Backend    │
│             │    {"account_ids": [...], "update_status"}  │   (Flask)    │
│             │ <────────────────────────────────────────── │              │
│             │    {"job_id": 42}   (immediate)             │  spawns      │
└─────────────┘                                             │  background  │
       │                                                    │  thread      │
       │  GET /api/accounts/healthcheck/42                  │      │      │
       │  (poll every 2s)                                    │      ▼      │
       │ ──────────────────────────────────────────────────>│  thread runs│
       │ <────────────────────────────────────────────────── │  check_*    │
       │  {"status":"running","progress":{"checked":3,"total":10}}         │
       │                                                    │  writes to   │
       │  ... (repeat)                                       │  DB on each  │
       │ ──────────────────────────────────────────────────>│  account     │
       │ <────────────────────────────────────────────────── │      │      │
       │  {"status":"success","results":[...]}               │      ▼      │
       └────────────────────────────────────────────────────┴──────────────┘
```

**Execution model:** Background Python `threading.Thread` in the Gunicorn worker.
- No Celery/Redis dependency (production deploy has no Celery workers)
- No new services
- Job state stored in SQLite via existing `HealthcheckRun` table (extended)

**Throttle:** Same as today — 60s cooldown between *starts* of healthcheck jobs (per `HealthcheckRun` last start).

---

## 2. Exact Files to Change

| File | Change |
|------|--------|
| `src/core/models.py` | Extend `HealthcheckRun`: add `results` (JSON), `progress` (JSON), `error_message` (Text). |
| `src/core/database.py` | Migration: add columns to `healthcheck_runs` if missing. |
| `src/dashboard/routes.py` | Add `POST /api/accounts/healthcheck/start` (returns job_id); add `GET /api/accounts/healthcheck/<int:job_id>` (status + results); refactor existing `accounts_healthcheck` to call shared logic or deprecate. |
| `src/clients/manager.py` | Add optional `progress_callback(checked: int, total: int, result: dict)` to `check_accounts_health`; when provided, callback after each account; when None, behavior unchanged. |
| `src/dashboard/templates/accounts.html` | Replace synchronous `checkAccounts()` with start → poll → render results flow. |
| `docs/FUNCTIONAL_AUDIT_ENDPOINTS.md` | Update endpoint docs. |

**New routes:**
- `POST /api/accounts/healthcheck/start` — start job, return job_id
- `GET /api/accounts/healthcheck/<job_id>` — poll status/results

**Deprecation:** `POST /api/accounts/healthcheck` remains during rollout as fallback; frontend switches to new flow. Can remove in a later phase.

---

## 3. Minimal Migration Plan

### Step 1: Schema (non-breaking)
Add to `HealthcheckRun`:
```python
results = Column(JSON, nullable=True)      # List[dict] when complete
progress = Column(JSON, nullable=True)    # {"checked": N, "total": M}
error_message = Column(Text, nullable=True)
```

Migration via `_ensure_healthcheck_run_columns()` in `database.py`, same pattern as `_ensure_accounts_healthcheck_columns()`.

### Step 2: Backend
1. Implement `check_accounts_health(..., progress_callback=...)` in manager.
2. Implement `/start` and `/jobs/<id>` routes.
3. Keep old `POST /api/accounts/healthcheck` working (calls same manager, no callback).

### Step 3: Frontend
1. Add feature flag or query param: `?use_jobs=1` — if present, use new flow.
2. Or: switch `checkAccounts()` to new flow by default; fallback to old if `start` returns 501/503.

### Step 4: Cleanup
1. Remove old sync endpoint (optional, later).
2. Remove truncation logic from manager when job-based path is default (truncation no longer needed).

---

## 4. Safe Rollout Plan

| Phase | Action | Validation |
|-------|--------|------------|
| **R1** | Deploy schema + backend; keep old route. New routes behind same `@admin_api_required`. | `curl -X POST .../healthcheck/start` → 200 + job_id. `curl .../healthcheck/42` → JSON. |
| **R2** | Deploy frontend with **opt-in**: add "Use background check (recommended)" checkbox; if checked, use new flow. | Manual test: check box, run healthcheck, verify results. |
| **R3** | Default to new flow; checkbox removed or hidden. Old sync route returns 410 Gone with message. | Full regression on /accounts. |
| **R4** | Remove truncation in manager for job path (check all accounts in job). | Verify many-account healthcheck completes without 524. |

**Rollback triggers:**
- Poll endpoint returning 500
- Job never completes (stuck "running")
- Frontend spinner never resolves

---

## 5. Rollback Plan

| Scenario | Rollback |
|----------|----------|
| **R2 — new flow broken** | Uncheck "Use background check"; users use old sync. Redeploy frontend without new UI. |
| **R3 — new flow is default, broken** | Revert frontend to call `POST /api/accounts/healthcheck` (sync) again. Backend still has old route. One-line JS change + redeploy. |
| **Schema migration failed** | Rollback migration (drop new columns if added); app continues with old columns as nullable. |
| **Thread dies / worker killed** | Job stays "running". Add `last_updated_at`; consider stale if >15 min. Frontend can show "Job may have failed; try again." Poll stops after N attempts or user closes modal. |

**Code rollback:** Revert commits; schema columns are additive (nullable), no downgrade required for quick rollback.

---

## 6. API Contract

### 6.1 Start job

**Request:**
```
POST /api/accounts/healthcheck/start
Content-Type: application/json
X-Admin-Token: <token>   # or session cookie
```

**Body:**
```json
{
  "account_ids": [1, 2, 3],   // optional; null/omit = all eligible
  "update_status": true,
  "verbose": false
}
```

**Response (201):**
```json
{
  "job_id": 42,
  "message": "Healthcheck started. Poll GET /api/accounts/healthcheck/42 for status."
}
```

**Error (429 — throttled):**
```json
{
  "error": "Throttled: healthcheck was started recently. Retry in ~45s.",
  "retry_after_sec": 45
}
```

**Error (401/403):** Same as existing admin API.

---

### 6.2 Get job status

**Request:**
```
GET /api/accounts/healthcheck/<job_id>
X-Admin-Token: <token>   # or session cookie
```

**Response — running:**
```json
{
  "job_id": 42,
  "status": "running",
  "progress": {
    "checked": 3,
    "total": 10,
    "current_account_id": 5
  },
  "results": [
    {"account_id": 1, "status": "alive", ...},
    {"account_id": 2, "status": "alive", ...},
    {"account_id": 3, "status": "error", "reason_code": "auth_required", ...}
  ],
  "started_at": "2025-03-14T12:00:00Z"
}
```

`results` is incremental: appended after each account. `progress` optional once total is known.

**Response — success:**
```json
{
  "job_id": 42,
  "status": "success",
  "results": [ /* full list */ ],
  "truncated": false,
  "total_eligible": 10,
  "started_at": "2025-03-14T12:00:00Z",
  "finished_at": "2025-03-14T12:02:30Z"
}
```

**Response — error:**
```json
{
  "job_id": 42,
  "status": "error",
  "error_message": "Connection failed for account 5",
  "results": [ /* partial */ ],
  "started_at": "...",
  "finished_at": "..."
}
```

**Response — timeout:**
```json
{
  "job_id": 42,
  "status": "timeout",
  "error_message": "Healthcheck timed out after 10 min",
  "results": [ /* partial */ ]
}
```

**Error (404):** Job not found or not accessible.

---

### 6.3 Polling behavior

- **Interval:** 2 seconds (same as QR login).
- **Stop when:** `status` ∈ `{success, error, timeout}`.
- **Stop after:** 600 seconds (10 min) or 300 polls.
- **Stale job:** If `status === "running"` and `started_at` > 15 min ago, show "Job may have timed out; try again."

---

## 7. Implementation Notes (Low-Risk)

### Thread safety
- Thread writes to DB via `get_db_context()`; SQLite handles single-writer.
- Each `progress_callback` does one `UPDATE healthcheck_runs SET progress=?, results=? WHERE id=?`.
- No shared in-memory state between request and thread.

### Manager change
- `progress_callback(checked: int, total: int, latest_result: dict)` called after each account.
- When `progress_callback` is None, behavior identical to current (no perf impact).

### Frontend
- Reuse existing modal, table, status badges, truncation note.
- Replace single `fetch(POST)` with: `fetch(POST start)` → `job_id` → `setInterval` calling `fetch(GET job_id)` until done.

---

## 8. Out of Scope

- Celery-based implementation (production has no Celery workers).
- WebSocket push (adds complexity; polling is sufficient).
- Partial results ordering (append-only; UI can sort by account_id if desired).
- Retry of failed accounts within same job (can be future enhancement).

---

## 9. Verification Commands

```bash
# Start job
curl -i -s -X POST -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{}' https://zellotex.com/api/accounts/healthcheck/start

# Poll status (replace 42 with job_id from above)
curl -i -s -H "X-Admin-Token: $TOKEN" https://zellotex.com/api/accounts/healthcheck/42

# Verify response is always JSON
curl -s -o /dev/null -w "%{content_type}" -H "X-Admin-Token: $TOKEN" \
  https://zellotex.com/api/accounts/healthcheck/42
# Expect: application/json
```

---

## 10. Summary

| Item | Value |
|------|-------|
| **Execution** | Background thread in Gunicorn worker |
| **Storage** | SQLite `healthcheck_runs` (extended) |
| **New infra** | None |
| **Auth** | Same `@admin_api_required` |
| **Breaking** | No; old sync route kept during rollout |
| **Risk** | Low; additive schema, optional frontend switch |
