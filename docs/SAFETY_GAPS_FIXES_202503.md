# Safety Gaps Fixes — Applied 2025-03

Applied the six audit gaps plus EXTRA GAP (counter semantics). Summary below.

---

## 1. Exact Files Changed

| File | Changes |
|------|---------|
| `src/core/safety_policy.py` | Block stale precheck (allowed or unknown) |
| `src/core/session_paths.py` | Precheck TTL, manual_review, story_precheck_stale in output |
| `src/stories/publisher.py` | Use get_story_safety_decision in _filter_eligible_account_ids; set last_story_failure_at on failure |
| `src/stories/run_batch.py` | Enforce max_accounts_per_story_batch, max_warming_accounts_per_batch |
| `src/dashboard/routes.py` | Batch caps in preview; story_precheck_stale in get_account |
| `src/dashboard/templates/accounts.html` | Precheck badge: "Expired" when allowed but stale |
| `src/core/warmup.py` | is_daily_cap_exceeded uses max_story_successes_per_day; is_cooldown_blocked uses last_story_failure_at |
| `src/core/database.py` | Add last_story_failure_at to _ensure_accounts_safety_columns |
| `src/core/models.py` | Add last_story_failure_at column to Account |
| `tests/test_safety_policy.py` | Add test_stale_allowed_precheck_blocked |

---

## 2. Patch Summary

### Gap 1 — Stale allowed precheck bypass
- **safety_policy.py**: Block when `precheck_stale` regardless of allowed/unknown. Stale "allowed" from days ago no longer permits publish.

### Gap 2 — get_story_availability ignores TTL and manual_review
- **session_paths.py**: Check precheck TTL; treat allowed+stale as blocked; include `manual_review_required` in `is_story_ready`; add `story_precheck_stale` to all return dicts.

### Gap 3 — Publisher uses different rules
- **publisher.py**: Replace `_filter_eligible_account_ids` with a pass through `get_story_safety_decision` so batch, single, and publisher share the same rules.

### Gap 4 — Batch caps not enforced
- **run_batch.py**: Use `max_accounts_per_story_batch` (default 10); cap warming/risky accounts via `max_warming_accounts_per_batch` (default 2).
- **routes.py**: Apply same caps in preview-batch.

### Gap 5 — Misleading precheck badge
- **accounts.html**: Use `story_precheck_stale`; show "Expired" instead of "OK" when precheck=allowed but stale.
- **routes.py**: Include `story_precheck_stale` in list_accounts and get_account.

### Gap 6 — Missing test
- **test_safety_policy.py**: Add `test_stale_allowed_precheck_blocked` to assert stale allowed blocks.

### EXTRA — Counter semantics
- **warmup.py**: `is_daily_cap_exceeded` uses `max_story_successes_per_day` (stories_today = successes). `is_cooldown_blocked` uses `last_story_failure_at` for a stronger cooldown after failure.
- **database.py, models.py**: Add `last_story_failure_at` migration and column.
- **publisher.py**: Set `last_story_failure_at` when a story publish fails.

---

## 3. Restart Commands

```bash
sudo systemctl restart autostory-web.service autostory-scheduler.service storyfleet-bot.service
```

---

## 4. Verification Commands

```bash
# Run safety policy tests
pytest tests/test_safety_policy.py -v

# Dry-run backfill (if needed)
python scripts/backfill_safety.py --dry-run

# Inspect story eligibility for account 36
curl -s "http://localhost:5000/api/accounts/36/story-eligibility" | jq

# Check that batch caps apply (preview should cap at 10 accounts, 2 warming)
curl -s -X POST http://localhost:5000/api/stories/preview-batch \
  -H "Content-Type: application/json" \
  -d '{"max_stories":20,"max_accounts":50}' | jq '.eligible_accounts | length'
# Should show at most 10 eligible

# Verify last_story_failure_at column exists (after migration)
sqlite3 data/storyfleet.db "PRAGMA table_info(accounts)" | grep last_story_failure
```

---

## 5. Assumptions Made

1. **run_batch.py** is the batch entrypoint — confirmed via scheduler worker and dashboard publish route.

2. **stories_today** is success-only; `max_story_successes_per_day` caps it. `max_story_attempts_per_day` is not enforced without a separate `story_attempts_today` counter.

3. **reset_daily_counters** (Celery) runs daily and resets `stories_today`; scheduling is outside this fix.

4. **last_story_failure_at** is set on any publish failure; cooldown uses the longer `story_failure_cooldown_minutes` when it is more recent than `last_story_attempt_at`.

5. **Preview-batch** warming cap uses the same logic as run_batch for consistency (safe accounts first, then up to max_warming warming/risky).

6. **Account model** uses `hasattr` for `last_story_failure_at` so older DBs without the column still work; migration adds the column on init_db.
