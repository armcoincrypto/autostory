# Status sync and bulk delete (healthcheck + dashboard)

## Summary of changes

### 1. Status write-back (status and health_status in sync)
- **`src/clients/manager.py`**
  - Added `_status_from_health(health_status, reason_code)` mapping:
    - `alive` → `active`
    - `frozen` + `RPCError_420` or `FloodWaitError` → `flood_wait`
    - `auth_required` → `auth_required`
    - `banned` → `banned`
    - `deleted` / `restricted` → `auth_required`
  - On every healthcheck **persist**, `Account.status` is set from the health result (no longer gated by `update_status`).
  - When result is **alive**, `last_error` is cleared.
  - `flood_wait_until` is set when we get FloodWait or RPC 420.

### 2. Safer healthcheck
- **Concurrency**: `HEALTH_CHECK_CONCURRENCY = 1` (was 3).
- **Jitter**: `HEALTH_JITTER_MIN_SEC` / `HEALTH_JITTER_MAX_SEC` (1–4 s) before each check.
- **Skip until flood_wait_until**: Accounts with `flood_wait_until > now` are skipped for this run.
- **Lightweight checks**: Only `is_user_authorized()` + `get_me()`; removed GetFullUser, GetAccountTTL, get_dialogs to reduce 420s.

### 3. Dashboard bulk delete for frozen accounts
- **API**: `POST /api/accounts/bulk-delete` with body `{ "account_ids": [1, 2, 3] }`. Deletes DB row, related stories, unlinks tasks, removes from client manager, deletes canonical session file. Requires admin.
- **UI** (Accounts page):
  - Checkbox per row (only frozen rows are checkable).
  - **Select all frozen**: checks all frozen rows.
  - **Delete selected**: deletes selected accounts (confirmation modal).
  - **Delete all frozen**: deletes all accounts with health=frozen or status=flood_wait (confirmation modal).

### 4. Dashboard clarity
- **List API**: `GET /api/accounts` returns `{ "accounts": [...], "summary": { "total", "active", "frozen", "auth_required", "banned", "other" } }`. Other pages that use `/api/accounts` accept both array and object with `accounts`.
- **Accounts table**: New **Health** column (health_status badge; tooltip = health_reason). Counts summary bar above table (Active, Frozen, Auth required, Banned, Other, Total).

---

## Syntax-check commands

```bash
cd /opt/autostory  # or project root
source venv/bin/activate
python -m py_compile src/clients/manager.py src/dashboard/routes.py
```

---

## Restart commands

```bash
sudo systemctl restart autostory-web storyfleet-bot autostory-scheduler
sudo systemctl status autostory-web storyfleet-bot autostory-scheduler --no-pager
```

---

## Healthcheck verification

1. Run healthcheck from dashboard with **Update status in DB** checked (or leave unchecked; status is always written when persist=True).
2. After run, confirm in DB:
   - `alive` + `all_checks_passed` → `status=active`
   - `frozen` + `RPCError_420` or FloodWait → `status=flood_wait`
   - `auth_required` / not_authorized → `status=auth_required`

```bash
sqlite3 /opt/autostory/data/storyfleet.db "SELECT id, status, health_status, health_reason FROM accounts ORDER BY id LIMIT 20;"
```

3. Accounts with `flood_wait_until` in the future should be skipped on the next healthcheck until that time has passed.

---

## Delete-frozen verification

1. In dashboard, open Accounts.
2. Confirm summary shows Frozen count and Health column shows `alive` / `frozen` / `auth_required` etc.
3. Click **Select all frozen** → only frozen rows get checked.
4. Click **Delete selected** → confirmation modal shows count → confirm → selected accounts disappear; session files removed.
5. Or click **Delete all frozen** → confirm → all frozen accounts deleted.

API check:

```bash
# List frozen account ids (example)
curl -s -X POST http://localhost:8000/api/accounts/bulk-delete \
  -H "Content-Type: application/json" \
  -d '{"account_ids":[999]}'
# 999 = example id; use real ids from the UI or DB
```

---

## Files changed

- `src/clients/manager.py` – status write-back, concurrency 1, jitter, skip flood_wait_until, lightweight check, `_status_from_health`
- `src/dashboard/routes.py` – `list_accounts` returns `{ accounts, summary }`, `_accounts_summary()`, `POST /api/accounts/bulk-delete`
- `src/dashboard/templates/accounts.html` – summary bar, Health column, checkboxes, Select all frozen / Delete selected / Delete all frozen, confirmation modal, `loadAccounts` uses `data.accounts` and `data.summary`
- `src/dashboard/templates/stories.html` – `/api/accounts` response handled as list or `data.accounts`
- `src/dashboard/templates/scheduler.html` – same for `/api/accounts`
- `src/dashboard/templates/index.html` – same for `/api/accounts`
