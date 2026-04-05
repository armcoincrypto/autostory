# Admin API Auth Fix - Production Security (2025-03-14)

## A. Root Cause Analysis

### Primary cause: Fail-open logic in production

The production server runs (or ran) **old fail-open code** in `_admin_api_allowed()`:

```python
# OLD (FAIL-OPEN - REMOVED)
if not cfg:
    return True   # <-- ALLOWS ALL when token not configured
```

**Code path causing 200 OK:**
1. `cfg = _admin_token_configured()` returns `None` because `DASHBOARD_ADMIN_TOKEN` is not seen by the process.
2. `if not cfg: return True` → admin API allows request.
3. Result: 200 OK for any request without auth.

### Secondary cause: Token not visible to runtime

Even after adding `DASHBOARD_ADMIN_TOKEN` to `.env`:

1. **Deploy excludes `.env`** – `deploy/update-server.sh` uses `rsync --exclude .env`. The server's `.env` is never overwritten by deploy. If you added the token only to local `.env`, the server never got it.

2. **Production must have both in `/opt/autostory/.env`:**
   - `ENVIRONMENT=production`  (otherwise `settings.environment` defaults to "development")
   - `DASHBOARD_ADMIN_TOKEN=<long-random-value>`

3. **systemd `EnvironmentFile`** – Uses `EnvironmentFile=-/opt/autostory/.env`. The `-` means optional; if the file is missing, startup still succeeds but no vars are loaded.

### Additional vulnerability: Unprotected scheduler API

**`/api/v1/*` (scheduler_api blueprint) had no admin protection.** All routes (targets, bindings, templates, schedule, jobs, deliveries) were open. The main `api` blueprint (`/api/*`) had `require_admin_api`; scheduler did not.

---

## B. Exact File-by-File Changes

### 1. `src/dashboard/routes.py` (already updated in workspace)

**Why:** Replace fail-open with fail-closed logic; constant-time compare; explicit production handling.

**Changes:**
- `_is_production_env()` – Treat as production unless explicitly `development`.
- `_admin_api_allowed()` – Fail closed: no token in production → deny; wrong token → deny; `hmac.compare_digest` for constant-time comparison.
- Explicit dev bypass only when `DASHBOARD_ALLOW_INSECURE_ADMIN_API=true` AND `ENVIRONMENT=development`.

### 2. `src/dashboard/scheduler_routes.py`

**Why:** Add admin protection so `/api/v1/*` is not open.

**Changes:**
- Import `_admin_api_allowed` from routes.
- `require_admin_scheduler_api` `before_request` runs before `maybe_proxy`; rejects requests without valid token/session with 403.

### 3. `src/dashboard/app.py`

**Why:** Warn when production runs without admin token.

**Changes:**
- Startup check: if `ENVIRONMENT=production` and no `DASHBOARD_ADMIN_TOKEN`, log warning.

### 4. `.env.example`

**Why:** Document required production settings.

**Changes:**
- Clarify that production needs `ENVIRONMENT=production` and `DASHBOARD_ADMIN_TOKEN`.
- Note that deploy does not overwrite server `.env`.

---

## C. Tests Added

Tests in `tests/test_admin_auth.py`:

| Test | Coverage |
|------|----------|
| `test_admin_api_no_token_denied_when_token_configured` | No token → 403 |
| `test_admin_api_wrong_token_denied` | Wrong token → 403 |
| `test_admin_api_correct_token_allowed` | Correct token → not 403 |
| `test_admin_api_health_always_allowed` | `/api/health` exempt |
| `test_admin_api_no_token_configured_production_denied` | No token configured in prod → 403 |
| `test_admin_api_allow_insecure_dev_bypass` | Dev bypass with explicit flag |
| `test_scheduler_api_no_token_denied_when_token_configured` | `/api/v1/*` no token → 403 |
| `test_scheduler_api_correct_token_allowed` | `/api/v1/*` correct token → allowed |

Run: `pytest tests/test_admin_auth.py -v`

---

## D. Shell Commands to Verify on Server

### 1. Confirm `.env` on server

```bash
ssh root@207.180.212.142 "grep -E 'DASHBOARD_ADMIN_TOKEN|ENVIRONMENT' /opt/autostory/.env 2>/dev/null || echo 'File missing or vars not set'"
```

Expect: `DASHBOARD_ADMIN_TOKEN=<your-value>` and `ENVIRONMENT=production`.

### 2. Check systemd env before deploy

```bash
ssh root@207.180.212.142 "sudo systemctl show autostory-web.service -p Environment -p EnvironmentFiles 2>/dev/null | head -20"
```

### 3. Verify process sees token (after deploy)

```bash
# Get main gunicorn PID
ssh root@207.180.212.142 "PID=\$(pgrep -f 'gunicorn.*wsgi:app' | head -1); echo \"PID: \$PID\"; [ -n \"\$PID\" ] && sudo cat /proc/\$PID/environ | tr '\\\\0' '\\\\n' | grep -E 'DASHBOARD_ADMIN_TOKEN|ENVIRONMENT' || echo 'Process not found'"
```

### 4. Test API auth (replace `YOUR_CORRECT_TOKEN`)

```bash
# No token → must be 403
curl -i -s http://127.0.0.1:8000/api/accounts/session-audit

# Wrong token → must be 403
curl -i -s http://127.0.0.1:8000/api/accounts/session-audit -H "X-Admin-Token: wrong-token"

# Correct token → 200 (or 500 if DB/files missing)
curl -i -s http://127.0.0.1:8000/api/accounts/session-audit -H "X-Admin-Token: YOUR_CORRECT_TOKEN"

# POST story-precheck
curl -i -s -X POST http://127.0.0.1:8000/api/accounts/story-precheck \
  -H "Content-Type: application/json" \
  -H "X-Admin-Token: YOUR_CORRECT_TOKEN" \
  -d '{"account_ids":[36]}'

# Scheduler API (must also require auth)
curl -i -s http://127.0.0.1:8000/api/v1/targets
curl -i -s http://127.0.0.1:8000/api/v1/targets -H "X-Admin-Token: YOUR_CORRECT_TOKEN"

# Health (must remain public)
curl -i -s http://127.0.0.1:8000/api/health
```

### 5. Check logs

```bash
ssh root@207.180.212.142 "sudo journalctl -u autostory-web.service -n 50 --no-pager"
```

---

## E. Risks / Rollout Notes

### Lockout risk

- If `DASHBOARD_ADMIN_TOKEN` is wrong or missing in production, all admin API calls (including dashboard calls) will get 403.
- Before deploy: confirm token in `/opt/autostory/.env` and that it matches the value used by UI/scripts.
- Use a token stored somewhere safe (password manager) for rollback.

### Rollout order

1. Ensure `/opt/autostory/.env` contains:
   - `ENVIRONMENT=production`
   - `DASHBOARD_ADMIN_TOKEN=<long-random-value>`
2. Deploy code (`./deploy/update-server.sh`).
3. Restart: `sudo systemctl restart autostory-web autostory-scheduler storyfleet-bot`.
4. Run verification curl commands above.
5. If dashboard stops working, try logging in with a session cookie; session auth should still work if there is a valid admin session.

### Rollback

1. If locked out: SSH to server, add or fix `DASHBOARD_ADMIN_TOKEN` in `/opt/autostory/.env`, then:
   ```bash
   sudo systemctl restart autostory-web
   ```
2. If code rollback needed: revert to previous commit and redeploy. Old fail-open code will allow unauthenticated access until fixed.

---

## Summary of Protected Routes

**`/api/*` (api blueprint):** All routes except `/api/health` protected by `require_admin_api`.

**`/api/v1/*` (scheduler_api blueprint):** All routes protected by `require_admin_scheduler_api`.

**Exempt:** `/api/health` only.
