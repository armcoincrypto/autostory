# STORYFLEET / Autostory — Full Project Understanding Report

**Generated:** Production audit for operator understanding  
**Server path:** `/opt/autostory`  
**Domain:** https://zellotex.com  
**Local:** http://127.0.0.1:8000  

---

## 1. High-Level Architecture

### What the System Is

STORYFLEET is a **Telegram account orchestration platform** that:

1. **Manages multiple Telegram accounts** — import, healthcheck, warmup, trust preservation  
2. **Publishes stories** — bulk story posting with mentions to discovered users  
3. **Runs scheduled messaging** — sends PROMO/INFO messages to groups/channels on a schedule  
4. **Discovers users** — scans public chats for mention candidates  

### Main Purpose

- **Operators** add accounts (TDATA, QR, phone, paste), monitor health, run story batches and scheduled messages  
- **Accounts** go through import → canonical session file → healthcheck → warmup → precheck → story-safe usage  

### Main Services / Processes

| Service | How Started | What It Does |
|---------|-------------|--------------|
| **autostory-web** | `gunicorn -w 2 -b 127.0.0.1:8000 --timeout 600 wsgi:app` | Flask dashboard + API. Serves HTML pages and `/api/*` JSON endpoints. Binds localhost only; nginx proxies public traffic. |
| **autostory-scheduler** | `python main.py scheduler` | In-process asyncio loop. Generates `ScheduledJob` rows, executes due jobs (sends messages), runs `StorySchedule` batch jobs. No Celery. |
| **storyfleet-bot** | `python main.py bot` | Telegram bot for basic commands and status. Optional; logs to `/var/log/storyfleet/`. |
| **Celery worker/beat** | `main.py worker`, `main.py beat` | Defined but **NOT used in current production systemd deploy**. Redis is present; Celery may be used elsewhere or for future use. |

### How Components Fit Together

```
┌─────────────────┐     HTTPS      ┌─────────────┐     proxy     ┌───────────────────┐
│   Operator      │ ──────────────>│ Cloudflare  │ ────────────>│ nginx :443 → :8000 │
│   Browser       │                │             │               └─────────┬─────────┘
└─────────────────┘                └─────────────┘                        │
                                                                           ▼
┌─────────────────┐                ┌─────────────┐               ┌───────────────────┐
│ autostory-web   │ ◄─────────────>│ SQLite DB   │               │ Gunicorn + Flask  │
│ (dashboard)    │   read/write   │ storyfleet  │               │ 2 workers          │
└────────┬────────┘                └──────┬──────┘               └───────────────────┘
         │                                 │
         │ session files                   │
         ▼                                 │
┌─────────────────┐                        │
│ data/sessions/  │ ◄──────────────────────┘
│ account_1.session│  (canonical Telethon sessions)
└────────┬────────┘
         │
         │ Telethon
         ▼
┌─────────────────┐     ┌─────────────────┐
│ autostory-      │     │ storyfleet-bot   │
│ scheduler       │     │ (optional)       │
│ (run jobs,      │     └─────────────────┘
│ story batches)  │
└─────────────────┘
```

- **Flask** serves dashboard pages (Jinja) and API (JSON). All `/api/*` routes require admin auth (session cookie or X-Admin-Token).  
- **Gunicorn** runs 2 workers. Timeout 600s.  
- **SQLite** (WAL mode, busy_timeout 30s). Single DB file for accounts, stories, discovery, scheduler models.  
- **Telethon** connects to Telegram. Sessions stored as `account_<id>.session` under `data/sessions/`.  

---

## 2. Functional Modules

### 2.1 Dashboard Auth / Login

**File:** `src/dashboard/auth_routes.py`  
**Route:** `GET/POST /login`, `GET /logout`  

- **Flow:** Form login → `DashboardUser` lookup by username → `check_password()` → `login_user()`  
- **Requirements:** `is_active=True`, `is_admin=True`  
- **Session:** Flask-Login cookie. Secure in production.  
- **API gate:** All `/api/*` go through `require_admin_api()` (before_request): valid `X-Admin-Token` **or** logged-in admin. No token = 403.  

### 2.2 Accounts Page

**File:** `src/dashboard/templates/accounts.html`  
**Route:** `/accounts` (web), `GET /api/accounts` (API)  

- **List:** DB read only. `has_session` = canonical file exists or `session_path` points to existing file.  
- **Summary:** General Healthy, Session Available, Story Eligible Now, Warmup Hold, Rate Limited, etc.  
- **Purpose:** `autostory` (stories only), `messaging` (scheduler only), `both`. Set via dropdown.  
- **Actions:** Check selected (1–2 sync), Check all (background job), bulk delete, set username/photo, session audit.  

### 2.3 Account Import Flow

| Method | Route | Handler | Result |
|--------|-------|---------|--------|
| **Phone + code** | `POST /api/accounts/auth/start`, `auth/complete` | `client_manager.start_phone_auth`, `complete_phone_auth` | Creates account, canonical file, `import_source=phone` |
| **QR login** | `POST /api/accounts/auth/qr-start`, `GET qr-check` | `start_qr_login`, `check_qr_login` (background thread) | Creates account, canonical file, polls until scan |
| **Session string** | `POST /api/accounts/import-session` | `client_manager.import_session_string` | Creates or updates by `user_id`/phone, canonical file, `import_source=paste` |
| **TDATA zip** | `POST /api/accounts/import-tdata` | `tdata_import.discover_candidates` → per-candidate `import_session_string` | Extracts zip, discovers tdata/session strings, imports each. `import_source=tdata_zip`. |

**Inspect (dry run):** `POST /api/accounts/inspect-tdata` — no import, returns candidate count and failed conversions.

### 2.4 Session Storage / Canonical Session Files

**File:** `src/core/session_paths.py`  

- **Dir:** `STORAGE_SESSIONS_DIR` (default `./data/sessions` → `/opt/autostory/data/sessions`)  
- **Canonical path:** `account_<id>.session` — Telethon SQLite session file  
- **Logic:** After auth/import, `_save_string_session_to_canonical_file()` writes canonical file and sets `session_path` in DB.  
- **Has session:** Canonical file exists **or** `session_path` points to existing file.  

### 2.5 Healthcheck System

**Sync (1–2 accounts):**  
- `POST /api/accounts/healthcheck` — requires `account_ids`, caps at 2  
- Blocks until done. Used for quick checks.  

**Background (all accounts):**  
- `POST /api/accounts/healthcheck/start` — returns `job_id` immediately  
- `GET /api/accounts/healthcheck/<job_id>` — poll for status, progress, results  
- Background thread runs `check_accounts_health` with `progress_callback`  
- Throttle: 60s between starts (shared by sync and background)  

**Health result:** `alive`, `auth_required`, `deleted`, `banned`, `frozen`, `restricted`, `flood_wait`, `error`.  
**Session valid:** Canonical file used + connect + auth + get_me passed.  

### 2.6 Story-Related Logic

**Files:** `src/core/session_paths.py` (get_story_availability), `src/core/warmup.py`, `src/core/safety_policy.py`, `src/stories/precheck.py`, `src/stories/batch_helpers.py`  

**Flow:**

1. **Session ready** — canonical file or `session_path` file exists  
2. **Warmup** — `imported_at` + `min_account_age_hours` (e.g. 72h tdata, 48h paste, 24h QR)  
3. **Precheck** — `POST /api/accounts/story-precheck` runs Telethon story-capability check, sets `story_precheck_status`, `story_precheck_checked_at`  
4. **Safety policy** — `get_story_safety_decision()` checks session, warmup, precheck TTL, manual_review, cooldowns, caps  
5. **Story eligible now** — `is_story_ready=True` when safety allows  

**Precheck TTL:** Display TTL 24h; for posting, `precheck_ttl_post_minutes` (15 min). Stale precheck → never ready.  

### 2.7 Discovery

**Routes:** `GET /api/discovery/sources`, `GET /api/discovery/users`, `POST /api/discovery/scan`, `GET /api/discovery/stats`  

- **Sources:** Uploaded mention sources (txt lists) and discovery sources (channels/groups)  
- **Scan:** Scans channels for users → `DiscoveredUser` rows  
- **Usage:** Story batches pick mention candidates from discovery + uploaded lists  

### 2.8 Campaigns

**Routes:** `GET/POST /api/campaigns`, `PUT/DELETE /api/campaigns/<id>`  

- **Model:** Name, description, targeting, templates, media. Used for story campaigns.  

### 2.9 Scheduler

**Blueprint:** `scheduler_api` — prefix `/api/v1`. Routes: `/api/v1/targets`, `/api/v1/bindings`, `/api/v1/templates`, `/api/v1/schedule/profile/<account_id>`, `/api/v1/schedule/rules/<account_id>`, `/api/v1/schedule/generate-now`, `/api/v1/schedule/jobs`, `/api/v1/jobs/run-now`, `/api/v1/deliveries`. Protected by `require_admin_scheduler_api` (same token/session as main API).  

- **Targets:** Chat targets (channels, groups). Add by username or invite link. Verify to resolve `tg_id`.  
- **Bindings:** Account ↔ target. `AccountTargetBinding` with `can_post`, `allowed_types`, `daily_cap`  
- **Templates:** `MessageTemplate` for PROMO/INFO. Scope: GLOBAL, ACCOUNT, TARGET, BINDING  
- **Profile:** `ScheduleProfile` per account — `is_enabled`, timezone, interval, daily cap  
- **Rules:** `ScheduleRule` — when to send (e.g. RANDOM window)  
- **Jobs:** `ScheduledJob` — due at `run_at`, executor sends via Telethon  
- **Purpose filter:** Accounts with `purpose=messaging` or `both` appear in scheduler account picker  

**Executor:** `execute_job()` — gets account, target, template, resolves entity, joins if needed, sends message. Uses `client_manager` with per-account locks.  

**Story schedule:** `StorySchedule` + `StoryTemplate` — scheduler worker runs `run_batch_async()` for due story schedules.  

### 2.10 Bot

**File:** `src/bot/bot.py`  

- Telegram bot. Basic status and commands. Points users to dashboard for Scheduler, bulk import, full UI.  

### 2.11 Key DB Models (Operational)

| Model | Purpose |
|-------|---------|
| **Account** | Telegram account. `session_path`, `status`, `health_status`, `story_status`, `warmup_status`, `purpose`, `imported_at`, `import_source` |
| **DiscoveredUser** | Users from scanned chats. Used for mentions in stories |
| **Story** | Published stories. Links to account, campaign |
| **Campaign** | Story campaign config |
| **StoryTemplate** | Story content template |
| **StorySchedule** | Scheduled story batch (template + media_path + run_at) |
| **ChatTarget** | Scheduler targets (channels/groups) |
| **AccountTargetBinding** | Account ↔ target for scheduler |
| **ScheduleProfile** | Per-account scheduler settings |
| **ScheduleRule** | When to send (PROMO, INFO, RANDOM window) |
| **ScheduledJob** | Due message job |
| **MessageDelivery** | Delivery log |
| **MessageTemplate** | Scheduler message templates |
| **HealthcheckRun** | Healthcheck audit + background job state |
| **DashboardUser** | Dashboard admin logins |

---

## 3. End-to-End Account Lifecycle

```
1. Import (TDATA/QR/phone/paste)
   → Account row created or updated
   → _save_string_session_to_canonical_file() writes data/sessions/account_<id>.session
   → session_path, imported_at, import_source, warmup_status set

2. Appears in /accounts
   → list_accounts returns has_session=true when canonical file exists
   → Session Available badge when canonical_ok

3. Healthcheck
   → Connect + auth + get_me. Sets health_status, health_reason, health_checked_at
   → session_valid=true in results when canonical file used and API passed
   → Does NOT test story publishing

4. Warmup
   → imported_at + min_account_age_hours (config per import source)
   → warmup_status: new | warming | warmed | risky | blocked
   → is_warmup_blocked() prevents story use until warmed

5. Precheck (optional before story)
   → POST /api/accounts/story-precheck
   → Tests story capability via Telethon
   → Sets story_precheck_status, story_precheck_checked_at
   → TTL: 15 min for posting; 24h for display

6. Story-safe usage
   → get_story_safety_decision() allows only when:
     - Session ready
     - Warmup passed
     - Precheck allowed (or stale → blocked)
     - Not manual_review_required
     - Cooldowns/caps respected

7. Scheduler-safe usage
   → purpose=messaging or both
   → account_has_canonical_session
   → Executor gets client per job, sends message
```

---

## 4. Operational Safety Model

### What “Alive” Means

- **health_status=alive** — Telegram API responds: connect + auth + get_me passed  
- Does **not** guarantee session file exists or story capability  

### What “Session Valid” Means

- Canonical file exists **and** healthcheck used it **and** connect+auth+get_me passed  
- In healthcheck results: `session_valid=true`  

### What “Story Eligible Now” Means

- `is_story_ready=True` from `get_story_availability`  
- Implies: session ready, warmup passed, precheck allowed and not stale, no manual review, cooldowns/caps OK  

### What “Warming” Means

- `warmup_status=warming` or `new`  
- `imported_at` within `min_account_age_hours`  
- Blocked from story posting until warmed  

### What Can Break Trust

- Re-importing wrong TDATA over working account (overwrites session)  
- Bulk username/photo changes (increases detection risk)  
- Posting stories immediately after bulk import  
- Ignoring rate limits / flood_wait  

### Dangerous Operator Mistakes

- Uploading TDATA for wrong account over an existing good one  
- Bulk-setting identical usernames/photos on many accounts  
- Disabling warmup or precheck checks  
- Using accounts before healthcheck passes  

---

## 5. Current Project Health

### Clearly Working

- Dashboard login (session + admin check)  
- X-Admin-Token for API  
- `/accounts` list with summary  
- Sync healthcheck (1–2 selected)  
- Background healthcheck (start + poll)  
- TDATA import (zip + paste session string)  
- Canonical session file creation  
- Checkbox selection for healthcheck (after fix)  
- SQLite thread affinity (check_same_thread=False) for background jobs  

### Partially Working / Needs Validation

- Story batch end-to-end (preview, execute, batch history)  
- Story schedules (scheduler worker runs them)  
- Scheduler messaging (execute_job, bindings, targets)  
- Discovery scan (depends on channel access)  
- Campaigns CRUD  

### Risky Areas

- Re-import overwrites without confirmation  
- Long-running import (1–2 min) — proxy timeouts if misconfigured  
- Story batch caps and canary mode — must be configured for safety  

### What to Test Next

- Story batch: preview → run on 1 account → verify story appears  
- Scheduler: add target, add binding, run job now  
- Discovery: add source, scan, verify users  
- Post-import healthcheck on new TDATA accounts  

---

## 6. File-by-File Map

### Routes

| File | Role |
|------|------|
| `src/dashboard/routes.py` | Main API (`/api/*`) and web routes. Accounts, stories, discovery, campaigns, healthcheck, import |
| `src/dashboard/scheduler_routes.py` | Scheduler API: targets, bindings, templates, profile, rules, jobs, run-now, deliveries |
| `src/dashboard/auth_routes.py` | Login, logout |
| `wsgi.py` | Gunicorn entry point |
| `main.py` | CLI: dashboard, bot, scheduler, worker, beat, init, add-account, status, check-accounts |

### Templates

| File | Role |
|------|------|
| `src/dashboard/templates/base.html` | Base layout |
| `src/dashboard/templates/login.html` | Login form |
| `src/dashboard/templates/index.html` | Dashboard home |
| `src/dashboard/templates/accounts.html` | Accounts list, import modals (phone, QR, TDATA), healthcheck modals |
| `src/dashboard/templates/stories.html` | Stories, batch, templates, schedules |
| `src/dashboard/templates/discovery.html` | Discovery sources, users |
| `src/dashboard/templates/scheduler.html` | Scheduler: targets, bindings, templates, profile, jobs |

### Client / Session Manager

| File | Role |
|------|------|
| `src/clients/manager.py` | Telethon client manager. `import_session_string`, `check_accounts_health`, `start_phone_auth`, `complete_phone_auth`, QR flow, `_save_string_session_to_canonical_file` |
| `src/core/session_paths.py` | Canonical paths, `account_has_canonical_session`, `get_story_availability`, `classify_account_readiness` |

### DB

| File | Role |
|------|------|
| `src/core/database.py` | Engine, SessionLocal, `get_db_context`, `init_db`, column migrations |
| `src/core/models.py` | Account, Story, DiscoveredUser, Campaign, Task, etc. |
| `src/core/scheduler_models.py` | ChatTarget, AccountTargetBinding, MessageTemplate, ScheduleProfile, ScheduleRule, ScheduledJob, StorySchedule, etc. |

### Story Logic

| File | Role |
|------|------|
| `src/stories/precheck.py` | Story precheck (Telethon) |
| `src/stories/batch_helpers.py` | Batch building, eligibility, caps |
| `src/stories/run_batch.py` | `run_batch_async` |
| `src/core/warmup.py` | Warmup status, `is_warmup_blocked` |
| `src/core/safety_policy.py` | `get_story_safety_decision` |

### Scheduler Logic

| File | Role |
|------|------|
| `src/scheduler/worker.py` | Main loop: generate jobs, execute due jobs, story schedules |
| `src/scheduler/executor.py` | `execute_job` — send message via Telethon |
| `src/scheduler/generator.py` | `generate_jobs_for_date` |
| `src/scheduler/provisioning.py` | `ensure_scheduler_defaults` after import |

### Import Logic

| File | Role |
|------|------|
| `src/core/tdata_import.py` | `safe_extract_zip`, `discover_candidates` |
| `src/core/tdata_convert.py` | `tdata_to_session_string`, `find_tdata_root`, `find_all_session_strings_in_extracted` |

### Helpers / Utilities

| File | Role |
|------|------|
| `config/settings.py` | Pydantic settings (telegram, database, dashboard, storage, warmup) |
| `src/utils/helpers.py` | `utc_now` and similar |
| `src/core/session_lock.py` | Per-account file locks |

---

## 7. Terminal Verification Commands

Run from server as root (or with sudo where needed). `$ADM` = real X-Admin-Token.

```bash
cd /opt/autostory
export ADM='YOUR_REAL_ADMIN_TOKEN'

# === Services ===
systemctl --no-pager status autostory-web.service
systemctl --no-pager status autostory-scheduler.service
systemctl --no-pager status storyfleet-bot.service 2>/dev/null || true

# === Routes (local) ===
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/ping
curl -s -H "X-Admin-Token: $ADM" http://127.0.0.1:8000/api/health | jq .

# === DB ===
sqlite3 /opt/autostory/data/storyfleet.db "SELECT COUNT(*) FROM accounts;"
sqlite3 /opt/autostory/data/storyfleet.db "SELECT id, phone_number, purpose, status, health_status FROM accounts LIMIT 5;"

# === Sessions directory ===
ls -la /opt/autostory/data/sessions/ | head -20
ls /opt/autostory/data/sessions/account_*.session 2>/dev/null | wc -l

# === Accounts summary (API) ===
curl -s -H "X-Admin-Token: $ADM" "http://127.0.0.1:8000/api/accounts?summary=1" | jq '.summary'

# === Healthcheck sync ===
curl -s -X POST -H "X-Admin-Token: $ADM" -H "Content-Type: application/json" \
  -d '{"account_ids":[1]}' http://127.0.0.1:8000/api/accounts/healthcheck | jq '.success, .results | length'

# === Healthcheck background ===
curl -s -X POST -H "X-Admin-Token: $ADM" -H "Content-Type: application/json" \
  -d '{}' http://127.0.0.1:8000/api/accounts/healthcheck/start | jq '.job_id'
# Then poll (replace JOB with actual id):
curl -s -H "X-Admin-Token: $ADM" http://127.0.0.1:8000/api/accounts/healthcheck/JOB | jq '.status, .progress'

# === Story-related fields ===
sqlite3 /opt/autostory/data/storyfleet.db \
  "SELECT id, story_status, story_precheck_status, warmup_status FROM accounts LIMIT 5;"

# === Scheduler ===
sqlite3 /opt/autostory/data/storyfleet.db "SELECT COUNT(*) FROM chat_targets;"
sqlite3 /opt/autostory/data/storyfleet.db "SELECT COUNT(*) FROM schedule_profiles;"

# === Logs ===
journalctl -u autostory-web.service --since "10 min ago" --no-pager | tail -80
journalctl -u autostory-scheduler.service --since "10 min ago" --no-pager | tail -40
```

---

## 8. Final Operator Summary

### What This Project Currently Does Reliably

- Dashboard login and admin session  
- API auth (X-Admin-Token and session)  
- Account list with session/health/story status  
- Sync healthcheck (1–2 selected)  
- Background healthcheck (all accounts, polling)  
- TDATA import (zip + session string paste)  
- Canonical session file creation  
- Account purpose (autostory / messaging / both)  

### What Must Still Be Validated

- Story batch: preview and run on real account  
- Scheduler: add target, binding, run job now  
- Discovery: add source, run scan  
- Post-import healthcheck on newly imported accounts  
- Story schedules (scheduler worker runs them)  

### What to Never Do Carelessly in Production

- Re-import TDATA over an existing working account without confirming  
- Bulk-set identical usernames or profile photos on many accounts  
- Post stories immediately after bulk TDATA import  
- Disable or shorten warmup for new accounts  
- Ignore `session_file_saved: false` in import results  
- Use default DASHBOARD_SECRET_KEY or leave DASHBOARD_ADMIN_TOKEN unset  
