# Safety Hardening Deliverable

## Files Changed

| File | Changes |
|------|---------|
| `src/core/models.py` | Added `last_story_success_at`, `story_attempts_today` to Account |
| `src/core/database.py` | Added `last_story_success_at`, `story_attempts_today` to `_ensure_accounts_safety_columns` |
| `src/queue/tasks.py` | `reset_daily_counters` resets `story_attempts_today` |
| `src/core/warmup.py` | Cooldown only on `last_story_failure_at`; added `is_attempt_cap_exceeded` |
| `src/core/safety_policy.py` | Attempt cap; precheck TTL 15 min for posting; cooldown uses `last_story_failure_at` |
| `config/settings.py` | Added `precheck_ttl_post_minutes=15` |
| `src/stories/publisher.py` | Safety gate before SendStoryRequest; `last_story_success_at`, `story_attempts_today` in success/fail paths |
| `src/stories/batch_helpers.py` | Added `get_story_eligible_accounts_for_batch` shared helper |
| `src/stories/run_batch.py` | Uses `get_story_eligible_accounts_for_batch` |
| `src/dashboard/routes.py` | Preview uses `get_story_eligible_accounts_for_batch` |
| `src/dashboard/templates/accounts.html` | Session Available, Story Eligible Now, Warmup Hold, Story Frozen, alive tooltip |
| `src/dashboard/templates/stories.html` | Session Available, Story Eligible Now, General Healthy tooltip |
| `tests/test_safety_policy.py` | New tests for stale precheck, manual_review, cooldown, precheck override, attempt cap, warming cap |

## Migration Names

No standalone Alembic migrations. Schema changes applied via `_ensure_accounts_safety_columns()` on startup (adds `last_story_success_at`, `story_attempts_today` if missing).

## Restart Commands

```bash
# Web (gunicorn/Flask)
sudo systemctl restart autostory-web

# Scheduler (if separate)
sudo systemctl restart autostory-scheduler

# Celery worker (for reset_daily_counters)
sudo systemctl restart storyfleet-scheduler
```

## Verification Commands

```bash
# 1. Run safety policy tests
pytest tests/test_safety_policy.py -v

# 2. Verify columns exist (SQLite)
sqlite3 data/storyfleet.db "PRAGMA table_info(accounts);" | grep -E "last_story_success_at|story_attempts_today"

# 3. Preview batch (should use shared helper)
curl -s -X POST http://localhost:5000/api/stories/preview-batch \
  -H "Content-Type: application/json" \
  -d '{"max_stories": 5, "mentions_per_story": 3}' | jq '.eligible_accounts | length'

# 4. Check dashboard labels (manual)
# Open https://zellotex.com/accounts - verify: Session Available, Story Eligible Now, Warmup Hold, Story Frozen
# Verify General Healthy tooltip: "Not enough for stories—session + precheck + warmup required."
```
