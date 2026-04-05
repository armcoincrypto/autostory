# STORYFLEET Operator Runbook

**Practical workflow to minimize Telegram risk.** See also [OPERATOR_SAFETY_GUIDE.md](OPERATOR_SAFETY_GUIDE.md) for policy details.

---

## 1. How to warm accounts

1. **Import** – Add account via TDATA, QR, phone, or paste.
2. **Wait** – Do **not** post stories or change username/photo immediately.
   - Default: 48h (paste), 72h (tdata), 24h (QR/phone).
   - Config: `WARMUP_MIN_ACCOUNT_AGE_HOURS`, `WARMUP_MIN_ACCOUNT_AGE_HOURS_TDATA`.
3. **Health check** – Run "Check if accounts are alive". Alive ≠ story-ready.
4. **Story precheck** – Run story precheck for the account. Default: 1 account (canary). Use `canary_batch_ok=true` for more.
5. **First story** – Use one account first (canary). Add `canary_batch_ok=true` to batch payload when scaling.

---

## 2. How to test one canary

1. **Select one account** – Pick the oldest warmed account.
2. **Story precheck** – POST `/api/accounts/story-precheck` with `{"account_ids": [36]}` (no `canary_batch_ok` needed for 1).
3. **Publish one story** – Use Batch Publish with max 1 account, or single Publish. Default canary caps batch at 1 unless `canary_batch_ok=true`.
4. **Verify** – Check story appears, no rate limit or frozen.

---

## 3. When to stop

- **Precheck returns frozen/restricted** – Stop. Do not use that account for stories.
- **Story publish returns rate limit** – Wait for `story_blocked_until`. Do not retry.
- **Multiple accounts fail** – Stop batch. Investigate before scaling.
- **Hourly caps hit** – Prechecks, publishes, bulk profile actions have per-hour limits. Wait.

---

## 4. What statuses mean

| Status | Meaning |
|-------|---------|
| **Story Eligible Now** | Can publish – session + active + precheck fresh + warmup passed + cooldown clear |
| **General Healthy** | Telegram API responds. Not enough for stories. |
| **Warmup Hold** | New/warming – blocked from story posting |
| **Story Frozen** | Telegram blocked; unblock time unknown |
| **Story Rate Limited** | Cooldown; exact date in `story_blocked_until` |
| **Manual review required** | Flagged; clear before bulk use |
| **Precheck expired** | Run story precheck again |

---

## 5. Canary mode (default)

- **Story precheck:** Max 1 account unless `canary_batch_ok=true`.
- **Story publish (batch):** Max 1 account unless `canary_batch_ok=true`.
- **Preview-first:** Bulk username and bulk photo require preview before execute.

---

## 6. Hourly limits (server-side)

| Action | Default limit |
|--------|---------------|
| Story prechecks | 20/hour |
| Story publishes | 15/hour |
| Bulk username + photo (combined) | 8/hour |

---

## 7. Restart commands

```bash
sudo systemctl restart autostory-web.service autostory-scheduler.service storyfleet-bot.service
```

---

## 8. Admin / Dashboard

### Access

- **URL:** `https://your-domain/` (e.g. https://zellotex.com/)
- **Auth:** Dashboard user + password (separate from Telegram accounts).
- **API (e.g. curl):** Session cookie or `X-Admin-Token: YOUR_TOKEN`.

### Accounts page — sync, fleet, story precheck

- **Check selected (sync, ≤10)** — General Telegram health only (connect / auth / `get_me`). Does **not** prove story posting.
- **Fleet check (all eligible)** — Same general check, runs in the background and persists progress; safe to leave the page.
- **Story readiness — controlled precheck** — Separate flow: `CanSendStory`-style check only; **does not upload media or publish**. Use when the table shows story precheck needed or when **Next step** says to run precheck.
- **Canary / caps** — Multi-account precheck from the UI asks for confirmation (maps to `canary_batch_ok`); hourly precheck limits still apply server-side.

### Story readiness — ops view (Accounts table)

- **Summary row** — Counts are computed in the browser from the same fields as the table (`story_ui_status`, `is_story_ready`, `manual_review_required`), up to 500 accounts per load (`?limit=500`). No extra Telegram calls.
- **Precheck / Eligible / Publish-ready / Frozen / Rate lim / Manual** — Filter the table for batch targeting. **Manual** takes precedence when `manual_review_required` is set, even if another story state exists. **Frozen** includes both `story_ui_status` frozen and restricted. **Publish-ready** is the same policy bucket as **Eligible** but only accounts whose **Purpose** is Stories or Both (excludes Messaging-only rows scheduler operators may mark as messaging-only).
- **Copy / export (no Telegram)** — **Copy visible IDs** uses the current filter. **Copy publish-ready IDs** lists all publish-ready accounts in the currently loaded page data (up to 500). **Export visible TSV** downloads id, phone, username, purpose, story fields for visible rows. Use these for run sheets or handoff; publishing still enforces server canary and hourly caps.
- **Publish prep workspace (Milestone F)** — Numbered workflow on Accounts: general health → story precheck → Publish-ready shortlist → **Canary suggestions** (heuristic rank on the loaded publish-ready pool using `risk_level`, `successful_story_count`, `imported_at`, identity-verify flag, etc.) → confirm **Next step** / Manual before scaling. An **inclusion/exclusion** block explains Publish-ready vs Eligible. **Canary 1–3** badges on ID cells mark top heuristic rows. **Copy top ID** / **Copy top 3 IDs** are clipboard-only (no Telegram). TSV export includes risk and successful-story columns for run sheets.
- **Select visible / First 10 / Clear** — Only affects **general** health checkboxes (sync). Story precheck still uses the precheck card (or API). **First 10** matches the sync healthcheck server cap.

### Verify services

```bash
# Web dashboard (gunicorn on 127.0.0.1:8000)
sudo systemctl status autostory-web --no-pager

# Scheduler, bot
sudo systemctl status autostory-scheduler storyfleet-bot --no-pager

# Logs (last 5 min)
journalctl -u autostory-web.service --since "5 min ago" --no-pager | tail -80
```

### nginx proxy

- Must proxy to `127.0.0.1:8000`.
- Set `proxy_read_timeout` ≥ 180s (Cloudflare 524 if origin times out).
- See `deploy/nginx-autostory.conf` for reference.

### Env vars (production)

- `DASHBOARD_SECRET_KEY` — strong random value
- `DASHBOARD_ADMIN_TOKEN` — for API auth (X-Admin-Token)

---

## 9. Admin / Dashboard (login and deploy)

### Login

- URL: `https://your-domain/` or `http://IP:8000/` (nginx proxies to 127.0.0.1:8000).
- Credentials: Dashboard admin user (DashboardUser table). Create via: `python -c "from src.core.database import get_db_context; from src.dashboard.models import DashboardUser; ..."` or migration.
- API auth: `X-Admin-Token` header or session cookie. Required for `/api/*` except `/api/health` in production.

### Services (canonical)

| Service | Purpose |
|---------|---------|
| `autostory-web.service` | Flask dashboard + gunicorn on 127.0.0.1:8000 |
| `autostory-scheduler.service` | `python main.py scheduler` — periodic tasks |
| `storyfleet-bot.service` | Telegram bot |

### Verify services

```bash
sudo systemctl status autostory-web autostory-scheduler storyfleet-bot --no-pager
curl -s http://127.0.0.1:8000/api/health
journalctl -u autostory-web.service -n 50 --no-pager
```

### Deploy / update

```bash
# From project root
./deploy/update-server.sh
# Or: rsync + ssh + systemctl restart
```
