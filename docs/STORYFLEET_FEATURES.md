# STORYFLEET – Full Feature List (Bot + Dashboard)

Every function is available in the **Telegram bot** and/or the **Web dashboard**. Same data and accounts for both.

---

## 1. Account management

| Action | Bot | Dashboard |
|--------|-----|-----------|
| Add account (phone + code + 2FA) | `/login` or button **📱 Login Account** / **📱 Add Another** | Accounts → Add Account → **Phone + Code** tab |
| Add account (session string) | `/import_session` or `/tdata` → paste string | Accounts → **Import from pasted string** (Session string box) |
| Add many accounts (zip with .session files) | — | Accounts → **Import from tdata** → upload zip |
| Get login code (for “Send code via Telegram”) | — | Accounts → **Get login code** on an account row |
| Add account via QR (no SMS) | — | Accounts → Add Account → **QR Code** tab |
| List accounts | `/accounts` or button **👥 Accounts** / **👥 View Accounts** | **Accounts** tab (table) |
| Set account status (active/inactive) | — | Accounts → toggle button per row |
| Set purpose (Stories / Messaging / Both) | — | Accounts → **Purpose** dropdown (for Scheduler “Select user”) |
| Cancel current operation | `/cancel` (login, scan, session import, publish) | — (refresh page if needed) |

---

## 2. Statistics

| Action | Bot | Dashboard |
|--------|-----|-----------|
| Overall stats | `/stats` or button **📊 Stats** | **Dashboard** (stats cards) |
| Shown: accounts (active/total), stories, discovered users, **available for mention**, **campaigns active** | Yes (both /stats and Stats button) | Dashboard: accounts, stories, users, campaigns. Discovery tab: total, mentioned, **available for mention**, this week |

---

## 3. Stories

| Action | Bot | Dashboard |
|--------|-----|-----------|
| Publish story (one account) | `/publish` → choose account → send media → send caption | **Publish Story** modal or **Stories** tab → Publish |
| Publish story to **all** accounts | `/publish` → **📤 Publish to ALL N Accounts** → media → caption | **Stories** → **Batch publish** |
| Publish another (after success) | Button **📤 Publish Another** (same flow as /publish: ALL + each account) | Publish again from modal or Stories |
| List published stories | — | **Stories** tab |
| Cancel publish flow | `/cancel` or inline **❌ Cancel** | — |

---

## 4. Discovery (scan & users)

| Action | Bot | Dashboard |
|--------|-----|-----------|
| Scan group/channel for users | `/scan` → send @group or t.me link | **Scan Channel** modal or **Discovery** → Scan Channel |
| Scan another group | Button **🔍 Scan Another** | Run scan again |
| View users (available for mention) | `/users` or button **👥 View Users** | **Discovery** tab (table + “Available for Mention” stat) |
| Cancel scan | `/cancel` | — |

---

## 5. Campaigns

| Action | Bot | Dashboard |
|--------|-----|-----------|
| List campaigns | `/campaigns` | **Campaigns** tab |
| Create campaign | — | Campaigns → **Create campaign** |
| Edit / activate / pause campaign | — | Campaigns → edit and toggle |

---

## 6. Scheduler (messaging to groups)

| Action | Bot | Dashboard |
|--------|-----|-----------|
| Targets (groups to message) | — | **Scheduler** → Targets |
| Templates (message text) | — | Scheduler → Templates |
| Bindings (account ↔ target) | — | Scheduler → Bindings |
| Schedule rules / run now | — | Scheduler → Run now, profile, rules |
| Use account for Scheduler | Set **Purpose** to Messaging or Both (dashboard only) | Accounts → Purpose = **Messaging** or **Both** |

Scheduler is **dashboard-only** (no bot commands). Bot `/help` and **❓ Help** point to the Dashboard for this.

---

## 7. Help & start

| Action | Bot | Dashboard |
|--------|-----|-----------|
| Welcome + command list | `/start` | — |
| Full command list + Dashboard note | `/help` or button **❓ Help** | — |

---

## Bot commands (quick reference)

- **Account:** `/login`, `/import_session`, `/tdata`, `/accounts`, `/cancel`
- **Stats:** `/stats`
- **Stories:** `/publish`
- **Discovery:** `/scan`, `/users`
- **Campaigns:** `/campaigns`
- **Help:** `/start`, `/help`

**Dashboard-only:** Bulk zip import, QR login, get login code, account status/purpose, Scheduler (targets, templates, bindings, run now), batch publish UI, campaign CRUD.

---

## Services & deploy

- **Bot:** `storyfleet-bot.service`
- **Dashboard:** `autostory-web.service` (Gunicorn, port 8000)
- **Deploy from your Mac:** `cd /path/to/autostory && ./deploy/update-server.sh`

All data (accounts, stories, discovered users, campaigns, scheduler) is shared; bot and dashboard use the same database and `client_manager`.
