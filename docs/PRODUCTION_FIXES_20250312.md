# Production-Safe Fixes - March 12, 2025

## Root Cause: Why Telegram Blocks Stories Even When Accounts Are Alive

**General health** (healthcheck: connect, get_me, is_user_authorized) and **story capability** are different:

1. **STORIES_TOO_MUCH** – Account is alive (API responds), but has hit the daily story quota. Telegram returns `retry_after` seconds. Wait that long, then retry.
2. **Frozen accounts** – "not available for frozen accounts" means Telegram has restricted story posting (e.g. abuse, inactivity, risk signals). No known unblock time.
3. **Restricted** – Similar to frozen; story posting disabled.

The dashboard now separates:
- **General Healthy** – health_status=alive
- **Session Ready** – canonical session file exists
- **Story Precheck** – CanSendStoryRequest result (strong hint)
- **Story Status** – Actual publish result (source of truth when available)
- **Story Available At** – Exact unblock time when known (from story_blocked_until)

---

## 1. Files Changed

| File | Purpose |
|------|---------|
| `src/clients/manager.py` | Import metadata: direct assignment for imported_at, first_seen_at, warmup_status, import_source on all paths (complete_phone_auth, import_session_string, QR) |
| `src/stories/precheck.py` | Extract retry_after from exception (seconds/retry_after) first; improved regex; never hardcode 24h when Telegram returns real value |
| `src/core/session_paths.py` | Story-ready: exclude precheck frozen/restricted/rate_limited/failed_check; show precheck reason when blocking |
| `src/stories/batch_helpers.py` | Exclude story_precheck_status in (frozen, restricted, rate_limited, failed_check, blocked) |
| `src/stories/publisher.py` | Same precheck exclusion in _filter_eligible_account_ids; extract retry_after from STORIES_TOO_MUCH in publish failure handler |
| `src/dashboard/routes.py` | Bulk username/photo: jitter between operations (get_safe_jitter_sec) |
| `scripts/backfill_import_metadata.py` | **NEW** – One-time backfill for legacy accounts with null metadata |

---

## 2. Full Code Diffs (Summary)

### A. Import metadata (manager.py)

- `complete_phone_auth` (existing): always set imported_at, first_seen_at (if null), warmup_status, import_source="phone"
- `complete_phone_auth` (new): import_source="phone" (was "tdata")
- `import_session_string` (existing): direct assignment, import_source = (import_source or "session_string")[:100]
- `import_session_string` (new): same
- `_qr_login_thread`: set imported_at, first_seen_at, warmup_status="new", import_source="qr"

### B. Precheck (precheck.py)

- Try e.seconds, e.retry_after first
- Regex patterns: wait X seconds, retry_after=X, STORIES_TOO_MUCH_(\d+), \b(\d{2,5})\b (60–86400*32)
- Only fallback to 86400 when extraction fails

### C. session_paths.py

- _PRECHECK_BLOCKING = {failed_check, frozen, restricted, rate_limited, blocked}
- precheck_ok = precheck not in _PRECHECK_BLOCKING
- display_reason uses story_precheck_reason when precheck is blocking
- frozen/restricted from story_status OR story_precheck_status

### D. batch_helpers.py + publisher

- Add story_precheck_status check: skip when in (frozen, restricted, rate_limited, failed_check, blocked)

### E. stories/publisher.py publish failure

- Extract retry_after: e.seconds, e.retry_after, or regex; use actual value, not hardcoded 24h

### F. Bulk jitter (routes.py)

- Import time, random; from src.core.warmup import get_safe_jitter_sec
- Between bulk username/photo ops: time.sleep(random.uniform(jmin, jmax))

---

## 3. Restart Commands

```bash
sudo systemctl restart autostory-web.service autostory-scheduler.service storyfleet-bot.service
```

---

## 4. SQL to Verify Metadata Persistence

```sql
-- After fresh import, these should be non-null
SELECT id, phone_number,
       imported_at, first_seen_at, warmup_status, import_source,
       story_precheck_status
FROM accounts
ORDER BY id
LIMIT 20;
```

---

## 5. SQL to Verify Story Precheck vs Story Status Separation

```sql
-- Precheck (hint) vs story_status (publish truth)
SELECT id,
       story_precheck_status, story_precheck_reason, story_precheck_checked_at,
       story_status, story_status_reason, story_status_checked_at,
       story_blocked_until
FROM accounts
WHERE story_precheck_status IS NOT NULL OR story_status IS NOT NULL
ORDER BY id;
```

---

## 6. SQL to Verify Story-Ready Calculation

```sql
-- Accounts that should be story-ready: precheck allowed/unknown, story_status ok/unknown, no block
SELECT id, story_precheck_status, story_status, story_blocked_until,
       warmup_status
FROM accounts
WHERE story_blocked_until IS NULL OR story_blocked_until < datetime('now')
ORDER BY id;
```

---

## 7. One-Time Backfill Command

```bash
# Dry run first
cd /opt/autostory
python scripts/backfill_import_metadata.py --dry-run

# Apply
python scripts/backfill_import_metadata.py

# Specific accounts only
python scripts/backfill_import_metadata.py --account-ids 13,14,15,16,17,18,19,20,21,36,61
```

---

## 8. Copy-Paste Test Commands (Server)

```bash
# Restart
sudo systemctl restart autostory-web.service autostory-scheduler.service storyfleet-bot.service

# Session audit
curl -s http://127.0.0.1:8000/api/accounts/session-audit | python3 -m json.tool | head -80

# Story precheck (replace YOUR_TOKEN)
curl -s -X POST http://127.0.0.1:8000/api/accounts/story-precheck \
  -H "Content-Type: application/json" \
  -H "X-Admin-Token: YOUR_TOKEN" \
  -d '{"account_ids":[13,14,15,16,17,18,19,20,21,36,61]}' | python3 -m json.tool

# Accounts list with summary
curl -s "http://127.0.0.1:8000/api/accounts?summary=1" | python3 -m json.tool | head -150

# DB: metadata
sqlite3 /opt/autostory/data/storyfleet.db "SELECT id, imported_at, warmup_status, story_precheck_status, story_blocked_until FROM accounts LIMIT 15;"

# Backfill (dry run)
cd /opt/autostory && python scripts/backfill_import_metadata.py --dry-run

# Backfill (apply)
cd /opt/autostory && python scripts/backfill_import_metadata.py
```
