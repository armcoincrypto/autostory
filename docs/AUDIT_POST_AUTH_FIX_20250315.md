# Autostory / Storyfleet – Post-Auth-Fix Security Audit

**Date:** 2025-03-15  
**Scope:** Security, correctness, operational robustness after admin auth and dashboard login fixes.

---

## A. What Is Healthy Now

| Area | Status |
|------|--------|
| **Admin API auth** | Fail-closed; `DASHBOARD_ADMIN_TOKEN` or session required; constant-time compare; `/api/health` exempt |
| **Dashboard login** | Auth blueprint with `/login`, `/logout`; `DashboardUser` + password; CSRF on login form |
| **Web route protection** | `/`, `/accounts`, `/stories`, `/discovery`, `/campaigns`, `/scheduler` use `@login_required` |
| **API route protection** | `/api/*` and `/api/v1/*` protected by `require_admin_api` / scheduler `before_request` |
| **403 frontend handling** | Fetch interceptor in `base.html` redirects to `/login` on 403 for `/api/*` |
| **Session/cookies** | Flask-Login; `credentials: 'same-origin'` for fetch; login form has CSRF |
| **Deployment** | `.env` excluded from rsync; `EnvironmentFile=-/opt/autostory/.env` in systemd |
| **Nginx** | Proxies to `127.0.0.1:8000`; long timeouts for TDATA; HTTPS via certbot |
| **Public assets** | `/favicon.ico`, `/ping`, `/api/health`, `/login`, `/static/*` correctly public |

---

## B. What Is Still Risky

| Risk | Severity | Description |
|------|----------|-------------|
| **404 handler bypasses auth** | HIGH | Unknown paths with `Accept: text/html` receive `index.html` (200) without `login_required`. User briefly sees dashboard shell before API 403s redirect to login. |
| **Gunicorn binds to 0.0.0.0:8000** | HIGH | All interfaces. If port 8000 is reachable, direct access bypasses nginx (no HTTPS, no nginx hardening). |
| **SECRET_KEY default** | HIGH | Default `change-me-in-production`. If `DASHBOARD_SECRET_KEY` is unset in production, session forging is possible. |
| **Session cookie not Secure** | MEDIUM | No explicit `SESSION_COOKIE_SECURE=True` in production. Cookie can be sent over HTTP (e.g. initial redirect). |
| **500 response handling** | MEDIUM | Many fetches do `const data = await response.json(); data.accounts.active` without `response.ok`. On 500, `data = {error: "..."}`; access to nested keys throws. 403 interceptor helps; 500 can cause JS errors. |
| **401 not handled** | LOW | API returns 403, not 401. If anything ever returns 401, it is not redirected to login. |
| **CSRF exempt on API** | LOW | `api` and `scheduler_api` exempt. Session-based browser calls are not CSRF-protected. Mitigated by same-origin and token header for scripts. |
| **SQLite WAL files** | LOW | `*.db-wal`, `*.db-shm` not in `.gitignore`; `data/locks/` not ignored. Risk of accidental commit. |

---

## C. High-Priority Fixes

1. **404 handler auth** – Ensure 404 for unknown paths does not serve dashboard without auth.
2. **Gunicorn bind** – Use `127.0.0.1:8000` when nginx is in front.
3. **SECRET_KEY validation** – Fail startup or warn in production if `DASHBOARD_SECRET_KEY` is default.
4. **SESSION_COOKIE_SECURE** – Set `True` when `X-Forwarded-Proto` is `https` or `ENVIRONMENT=production`.

---

## D. Medium-Priority Fixes

1. **Frontend 500 resilience** – Check `response.ok` before parsing and using JSON; show a clear error message instead of accessing nested keys.
2. **401 in fetch interceptor** – Treat 401 like 403 and redirect to `/login`.
3. **Gitignore** – Add `*.db-wal`, `*.db-shm`, `data/locks/`.

---

## E. Exact File-by-File Changes Recommended

### E.1. `src/dashboard/app.py`

**1. 404 handler – require auth before serving index.html**

```python
@app.errorhandler(404)
def not_found(error):
    from flask import request
    if request.path.startswith('/api') or request.path.startswith('/static'):
        return {"error": "Not found", "detail": "Not Found"}, 404
    accept = request.headers.get('Accept', '') or ''
    if 'text/html' in accept:
        from flask_login import current_user
        if not current_user.is_authenticated or not getattr(current_user, 'is_admin', False):
            from flask import redirect, url_for
            return redirect(url_for('auth.login', next=request.path))
        from flask import render_template
        return render_template('index.html'), 200
    return {"error": "Not found", "detail": "Not Found"}, 404
```

**2. Session cookie security (add after `csrf.init_app(app)`):**

```python
    if (settings.environment or "").strip().lower() == "production":
        app.config["SESSION_COOKIE_SECURE"] = True
        app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
```

**3. SECRET_KEY validation (add after app creation, before `return app`):**

```python
    if (settings.environment or "").strip().lower() == "production":
        sk = (settings.dashboard.secret_key or "").strip()
        if not sk or sk.lower() in ("change-me-in-production", "generate-a-secure-random-key-here"):
            logger.error("DASHBOARD_SECRET_KEY must be set to a strong random value in production")
            raise ValueError("DASHBOARD_SECRET_KEY required in production")
```

### E.2. `deploy/autostory-web.service`

**Gunicorn bind to localhost only (when nginx proxies):**

```diff
- ExecStart=/opt/autostory/venv/bin/gunicorn -w 2 -b 0.0.0.0:8000 --timeout 600 wsgi:app
+ ExecStart=/opt/autostory/venv/bin/gunicorn -w 2 -b 127.0.0.1:8000 --timeout 600 wsgi:app
```

### E.3. `src/dashboard/templates/base.html`

**Extend fetch interceptor for 401:**

```javascript
                if (typeof url === 'string' && url.indexOf('/api/') !== -1 && (r.status === 403 || r.status === 401)) {
```

### E.4. `.gitignore`

**Add:**

```
# SQLite WAL files
*.db-wal
*.db-shm
data/locks/
```

### E.5. Frontend 500 resilience (representative fix)

**`src/dashboard/templates/index.html` – `refreshStats`:**

```javascript
async function refreshStats() {
    try {
        const response = await fetch('/api/stats');
        const stats = await response.json();
        if (!response.ok) {
            document.getElementById('stat-accounts').textContent = '—';
            document.getElementById('stat-stories').textContent = '—';
            document.getElementById('stat-users').textContent = '—';
            document.getElementById('stat-campaigns').textContent = '—';
            console.error('Stats failed:', stats.error || response.statusText);
            return;
        }
        document.getElementById('stat-accounts').textContent = (stats.accounts && stats.accounts.active) ?? '—';
        document.getElementById('stat-stories').textContent = (stats.stories && stats.stories.total) ?? '—';
        document.getElementById('stat-users').textContent = (stats.users && stats.users.discovered) ?? '—';
        document.getElementById('stat-campaigns').textContent = (stats.campaigns && stats.campaigns.active) ?? '—';
    } catch (error) {
        console.error('Failed to load stats:', error);
    }
}
```

(Apply the same pattern to `loadRecentStories` and other critical fetches on index, accounts, stories, discovery, campaigns, scheduler.)

---

## F. Exact Shell Commands to Verify Each Recommendation

### F.1. 404 handler auth bypass

```bash
# Before fix: Unauthenticated request returns 200 with HTML
curl -i -s -H "Accept: text/html" http://127.0.0.1:8000/nonexistent-path
# Expect before fix: 200 + dashboard HTML
# Expect after fix: 302 to /login?next=%2Fnonexistent-path
```

### F.2. Gunicorn bind

```bash
# Check what gunicorn is bound to
ss -tlnp | grep 8000
# Or:
lsof -i :8000 | head -5
# Expect: 127.0.0.1:8000 (not 0.0.0.0:8000) after fix

# Verify nginx still reaches it
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/api/health
# Expect: 200
```

### F.3. SECRET_KEY validation

```bash
# Simulate production without SECRET_KEY override
cd /opt/autostory
ENVIRONMENT=production DASHBOARD_SECRET_KEY=change-me-in-production python -c "
from src.dashboard.app import create_app
create_app()
" 2>&1
# Expect after fix: ValueError or exit 1
```

### F.4. Session cookie Secure flag

```bash
# After login, inspect Set-Cookie
curl -i -s -c - -b - -X POST http://127.0.0.1:8000/login \
  -d "username=admin&password=YOUR_PASS&csrf_token=TOKEN&next=/" 2>&1 | grep -i set-cookie
# Over HTTPS: Expect Secure attribute
# (May need to test via browser DevTools → Application → Cookies)
```

### F.5. Frontend 500 resilience

```bash
# Simulate 500 (e.g. temp DB error) – manual test
# In browser: Network tab, block /api/stats and return 500, or use service worker
# Expect: Stats show "—", no JS crash, console has error
```

### F.6. Gitignore

```bash
cd /opt/autostory
git status --short data/
# Expect: data/locks/, *.db-wal, *.db-shm not reported as untracked after gitignore update
```

### F.7. Port 8000 exposure

```bash
# From another machine (if possible) or from host
nc -zv YOUR_SERVER_IP 8000 2>&1
# If nginx is only public entry: Expect "Connection refused" or timeout (port filtered)
# If 8000 is open: Risk – fix Gunicorn bind and firewall
```

---

## Summary

| Priority | Count | Main items |
|----------|-------|------------|
| High    | 4     | 404 auth bypass, Gunicorn bind, SECRET_KEY default, session Secure |
| Medium  | 3     | 500 handling, 401 interceptor, gitignore |
| Low     | 2     | CSRF exempt risk, 401 edge case |

After applying these changes, run the verification commands in section F and the existing auth and login tests.
