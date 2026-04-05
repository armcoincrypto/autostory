# Production Hygiene Audit

**Date:** 2025-03-14  
**Scope:** Temp/debug artifacts, deploy config alignment, UI wording, operator runbook

---

## 1. Audit Findings

### 1.1 Stale temp/debug artifacts

| Item | Status | Action |
|------|--------|--------|
| `.bak` / `.old` / `~` files in project | None found (excl. venv) | None |
| `print(` debug statements | None in src/ | None |
| `console.log` in templates | Removed in prior pass | None |
| `data/locks/*.lock` | In .gitignore | Already ignored |
| `*.db-shm`, `*.db-wal` | In .gitignore | Already ignored |

### 1.2 Deploy config vs repo expectations

| Config | Repo | Deployed (per update-server.sh) | Verdict |
|--------|------|---------------------------------|---------|
| **autostory-web.service** | `-b 127.0.0.1:8000 --timeout 600 wsgi:app` | ✅ Installed, restarted | **Canonical** |
| **storyfleet-dashboard.service** | `gunicorn.conf.py`, bind 0.0.0.0:5000 | ❌ Not installed | **Stale — remove** |
| **autostory-scheduler.service** | `main.py scheduler` | ✅ Installed | Canonical |
| **storyfleet-scheduler.service** | `User=storyfleet`, file logging | ❌ Not installed | **Stale — remove** |
| **storyfleet-bot.service** | Bot process | ✅ Installed | Canonical |
| **nginx** | proxy to 127.0.0.1:8000 | Per nginx-*.conf | Align with deploy |

**Canonical services (update-server.sh):** autostory-web, autostory-scheduler, storyfleet-bot.

### 1.3 Dashboard wording vs backend truth

| UI text | Backend | Verdict |
|---------|---------|---------|
| General Healthy | health_status=alive | ✅ Match |
| Session Available | canonical session file exists | ✅ Match |
| Story Eligible Now | is_story_ready (safety policy) | ✅ Match |
| Healthcheck: "Session Valid" | API `session_valid` (alive + canonical used) | ✅ Correct in context |
| Healthcheck: "Story Ready" | Inconsistent with "Story Eligible Now" | ⚠️ Fix → "Story Eligible Now" |

### 1.4 Deploy docs

| File | Purpose | Verdict |
|------|---------|---------|
| **DEPLOYMENT.md** | Main deploy guide | Keep |
| **deploy/update-server.sh** | Incremental deploy script | Keep |
| **DEPLOY_CLEAN.md** | Clean deploy (rm -rf, full replace) | Keep — distinct use case |
| **DEPLOY_FRESH.md** | Alternate fresh deploy | Keep |
| **DEPLOY_COMMANDS.md** | Command reference | Keep or merge into DEPLOYMENT |

### 1.5 Operator runbook

- **OPERATOR_RUNBOOK.md** — Operator workflow (warm, canary, limits). Good.
- **Missing:** Admin/dashboard section (login, services, logs, verification).

---

## 2. Exact Files to Change

| File | Action |
|------|--------|
| `deploy/storyfleet-dashboard.service` | **Remove** (stale; autostory-web is canonical) |
| `deploy/storyfleet-scheduler.service` | **Remove** (stale; autostory-scheduler is canonical) |
| `src/dashboard/templates/accounts.html` | Fix "Story Ready" → "Story Eligible Now" in healthcheck help |
| `docs/OPERATOR_RUNBOOK.md` | Add §8 Admin / dashboard section |

---

## 3. Diffs

### 3.1 Remove stale services

```diff
# Delete files (no replacement)
- deploy/storyfleet-dashboard.service
- deploy/storyfleet-scheduler.service
```

### 3.2 accounts.html wording

```diff
--- a/src/dashboard/templates/accounts.html
+++ b/src/dashboard/templates/accounts.html
@@ -283,7 +283,7 @@
-                    <summary class="text-muted small">Operator help: General Healthy vs Session Valid vs Story Ready</summary>
+                    <summary class="text-muted small">Operator help: General Healthy vs Session Valid vs Story Eligible Now</summary>
                     <div class="card card-body bg-dark border-secondary small mt-1">
                         <strong>General Healthy</strong> — health_status=alive. Connect+auth+get_me passed. Not enough for stories—session + precheck + warmup required.<br>
                         <strong>Session Valid</strong> — canonical file exists + connect+auth+get_me passed (shown as session_valid: true in results).<br>
-                        <strong>Story Eligible Now</strong> — session valid + active + story_status ok/unknown + not blocked. Only verified when attempting a story publish.
+                        <strong>Story Eligible Now</strong> — session valid + active + story_status ok/unknown + not blocked. Only verified when attempting story publish.
```

### 3.3 OPERATOR_RUNBOOK.md — add Admin section

```diff
 ---
 ## 7. Restart commands
@@ -70,3 +70,40 @@
 sudo systemctl restart autostory-web.service autostory-scheduler.service storyfleet-bot.service
 ```

+---
+
+## 8. Admin / dashboard
+
+- **Login:** Dashboard user/password (configured in DB). Separate from Telegram account auth.
+- **API auth:** Session cookie (browser) or `X-Admin-Token` header.
+- **Services:**
+  - `autostory-web` — Flask dashboard + API (Gunicorn on 127.0.0.1:8000)
+  - `autostory-scheduler` — Scheduler worker
+  - `storyfleet-bot` — Bot process
+- **Logs:**
+  ```bash
+  journalctl -u autostory-web.service --since "1 hour ago" --no-pager
+  journalctl -u autostory-scheduler.service --since "1 hour ago" --no-pager
+  tail -f /var/log/storyfleet/bot.log
+  ```
+- **Verify dashboard:**
+  ```bash
+  curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/ping
+  # Expect: 200
+  ```
+- **Nginx:** Proxies to 127.0.0.1:8000. Ensure `proxy_read_timeout` ≥ 180s for slow API calls.
```

---

## 4. Verification Commands

```bash
# After applying changes:

# 1. Confirm stale services removed
ls deploy/storyfleet-dashboard.service 2>/dev/null && echo "REMOVE: stale file exists" || echo "OK: removed"
ls deploy/storyfleet-scheduler.service 2>/dev/null && echo "REMOVE: stale file exists" || echo "OK: removed"

# 2. Confirm canonical services used on server
ssh root@207.180.212.142 'systemctl is-active autostory-web autostory-scheduler storyfleet-bot 2>/dev/null | paste -sd " " -'

# 3. Verify dashboard ping
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/ping
# Expect: 200

# 4. Smoke tests
pytest tests/test_dashboard_login.py -v
```

---

## 5. Rollback Notes

- **Removed service files:** If you previously used `storyfleet-dashboard` or `storyfleet-scheduler`:
  - Restore from git: `git checkout HEAD -- deploy/storyfleet-dashboard.service deploy/storyfleet-scheduler.service`
  - Do **not** re-enable them unless you intend to migrate; `autostory-web` and `autostory-scheduler` are canonical.
- **accounts.html:** Revert wording if needed: `git checkout HEAD -- src/dashboard/templates/accounts.html`
- **OPERATOR_RUNBOOK.md:** Revert: `git checkout HEAD -- docs/OPERATOR_RUNBOOK.md`
