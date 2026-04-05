# Dashboard Login Fix - 2025-03-15

## A. Root Cause

**The dashboard UI broke because:**

1. **No admin login route existed.** `login_manager.login_view = 'auth.login'` referenced a route that was never implemented. The `auth` blueprint did not exist. Flask-Login would redirect unauthenticated users to a 404.

2. **Web routes were unprotected.** Pages `/`, `/accounts`, `/stories`, etc. had no `@login_required`. They served HTML; the frontend then called `/api/stats`, `/api/accounts`, etc. The API (correctly) returned 403 without admin session or X-Admin-Token.

3. **Frontend assumed successful JSON.** Code like `const stats = await response.json(); document.getElementById('stat-accounts').textContent = stats.accounts.active;` runs on 403 responses. The 403 body is `{"success": false, "error": "Admin only"}`, so `stats.accounts` is undefined and `stats.accounts.active` throws, crashing the page.

4. **`/api/accounts/auth/*` is for Telegram accounts, not dashboard admins.** Those routes add Telegram user accounts to the system. Dashboard admin auth (DashboardUser, password) was never wired up.

---

## B. File-by-File Patches

### 1. `src/dashboard/auth_routes.py` (NEW)

**Purpose:** Implement admin login/logout. Separate from Telegram account auth.

```python
"""
Dashboard admin login/logout. Separate from Telegram account auth (/api/accounts/auth/*).
"""
from flask import Blueprint, redirect, render_template, request, url_for
from flask_login import login_user, logout_user, current_user

from src.core.database import get_db_context
from src.dashboard.models import DashboardUser

auth = Blueprint("auth", __name__)

@auth.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated and getattr(current_user, "is_admin", False):
        return redirect(request.args.get("next") or "/")
    if request.method == "GET":
        return render_template("login.html", next_url=request.args.get("next") or "/")
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    if not username or not password:
        return render_template("login.html", error="Username and password required", next_url=request.form.get("next") or "/")
    with get_db_context() as db:
        user = db.query(DashboardUser).filter(DashboardUser.username == username).first()
        if not user or not user.check_password(password):
            return render_template("login.html", error="Invalid username or password", next_url=request.form.get("next") or "/")
        if not getattr(user, "is_active", True):
            return render_template("login.html", error="Account disabled", next_url=request.form.get("next") or "/")
        if not getattr(user, "is_admin", False):
            return render_template("login.html", error="Access denied", next_url=request.form.get("next") or "/")
        login_user(user, remember=True)
    next_url = (request.form.get("next") or "/").strip()
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = "/"
    return redirect(next_url)

@auth.route("/logout")
def logout():
    logout_user()
    return redirect(url_for("auth.login"))
```

### 2. `src/dashboard/templates/login.html` (NEW)

Standalone login form with CSRF, dark theme, minimal layout.

### 3. `src/dashboard/app.py`

- Register auth blueprint: `app.register_blueprint(auth)`
- Add `@login_required` to `index_root` (app's `/` route)

### 4. `src/dashboard/routes.py`

- Add `@login_required` to all web blueprint routes: `index`, `accounts_page`, `stories_page`, `discovery_page`, `campaigns_page`, `scheduler_page`
- Leave `favicon` public

### 5. `src/core/database.py`

- In `init_db()`, add: `import src.dashboard.models  # noqa: F401` so `dashboard_users` table is created

### 6. `src/dashboard/templates/base.html`

- Add 403 fetch interceptor: when any `fetch()` to `/api/*` returns 403, redirect to `/login?next=<current-path>`
- Add Logout link in sidebar: `<a href="{{ url_for('auth.logout') }}">Log out</a>`

### 7. `scripts/create_admin.py` (NEW)

CLI to create first admin user. Usage:

```bash
python scripts/create_admin.py admin YOUR_SECURE_PASSWORD
# Or: ADMIN_USER=admin ADMIN_PASSWORD=secret python scripts/create_admin.py
```

---

## C. Exact Test Plan

1. **Unauthenticated redirect**
   - GET `/` without session → 302 to `/login?next=%2F`
   - GET `/accounts` → 302 to `/login?next=%2Faccounts`

2. **Login page**
   - GET `/login` → 200, shows login form

3. **Login flow**
   - Run `python scripts/create_admin.py admin admin@local secret123`
   - POST `/login` with `username=admin`, `password=secret123` → 302 to `/`
   - GET `/` with session cookie → 200, dashboard loads
   - GET `/api/stats` with session cookie → 200 (admin session passes `_admin_api_allowed`)

4. **Logout**
   - GET `/logout` → 302 to `/login`
   - Subsequent GET `/` → 302 to `/login`

5. **API with X-Admin-Token (unchanged)**
   - `curl -H "X-Admin-Token: $TOKEN" /api/accounts` → 200
   - `curl /api/accounts` (no token, no session) → 403

6. **403 frontend handling**
   - Load dashboard, then expire session (or clear cookie). Trigger an API call (e.g. Refresh). Page redirects to `/login` instead of crashing.

---

## D. Exact Commands to Verify

### Browser

```bash
# 1. Create first admin (run once)
cd /opt/autostory
source venv/bin/activate
python scripts/create_admin.py admin admin@local YOUR_SECURE_PASSWORD

# 2. Open browser (incognito to avoid cached session)
# Visit http://YOUR_SERVER:8000/

# 3. Expect: redirect to /login
# 4. Log in with admin / YOUR_SECURE_PASSWORD
# 5. Expect: redirect to /, dashboard loads with stats
# 6. Click Log out
# 7. Expect: redirect to /login
```

### Curl

```bash
# No auth -> 403
curl -i -s http://127.0.0.1:8000/api/stats
# Expect: HTTP/1.1 403

# X-Admin-Token -> 200 (when DASHBOARD_ADMIN_TOKEN set)
curl -i -s -H "X-Admin-Token: YOUR_TOKEN" http://127.0.0.1:8000/api/stats
# Expect: HTTP/1.1 200

# Session cookie (after browser login, copy cookie)
curl -i -s -b "session=COOKIE_VALUE" http://127.0.0.1:8000/api/stats
# Expect: HTTP/1.1 200

# Unauthenticated GET / -> redirect to login
curl -i -s -L http://127.0.0.1:8000/
# Expect: 302 to /login, then 200 (login page)
```

---

## E. Risks and Rollout

| Risk | Mitigation |
|------|-------------|
| No admin user exists | Run `create_admin.py` before or right after deploy |
| Lockout | Keep a second admin or know the password; use X-Admin-Token for scripts |
| Session cookie domain/path | Same-origin; ensure DASHBOARD_HOST/port and cookies align |

**Rollout order**

1. Deploy code
2. Run `python scripts/create_admin.py admin admin@local <password>` on server
3. Restart `autostory-web.service`
4. Verify in browser: visit `/` → redirected to `/login` → log in → dashboard works
