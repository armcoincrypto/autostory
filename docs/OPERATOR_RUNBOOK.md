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

## 8. Admin / Dashboard

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
curl -s http://127.0.0.1:8000/ping
journalctl -u autostory-web.service -n 50 --no-pager
```

### Deploy / update

```bash
# From project root
./deploy/update-server.sh
# Or: rsync + ssh + systemctl restart
```
