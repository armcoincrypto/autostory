# Operator-Safety Review (Post-Hardening)

## Gaps Found

### 1. Operator-Risk Controls

| Gap | Location | Severity |
|-----|----------|----------|
| `get_story_availability` omits `is_attempt_cap_exceeded` | `src/core/session_paths.py` | High – UI shows "ready" when attempt cap exceeded; preview/execution diverge |
| `get_story_availability` uses `precheck_ttl_minutes` (24h) for is_story_ready | `src/core/session_paths.py` | High – UI "ready" when precheck 20 min old; safety gate blocks (15 min post TTL) |
| No per-account cooldown in bulk username/photo | `src/dashboard/routes.py` | Medium – Config has username_change_cooldown_hours, profile_photo_change_cooldown_hours but routes ignore |
| No cap on same-import batch reuse | `src/stories/batch_helpers.py` | Low – 20 accounts from same tdata_zip could all run in one batch |

### 2. Manual Override Safety

| Gap | Location | Severity |
|-----|----------|----------|
| No API to clear `manual_review_required` | `src/dashboard/routes.py` | Medium – Operators must use raw DB; no audit, no reason |
| PATCH account allows only `purpose` | `src/dashboard/routes.py` | – |

### 3. Warmup Enforcement

| Status | Note |
|--------|------|
| ✓ | Warming cap in batch (max_warming_accounts_per_batch) enforced |
| ✓ | `get_story_safety_decision` checks warmup before allowing |
| ✓ | Allowed precheck does not bypass warmup/cooldown/attempt-cap |

### 4. Telegram-Facing Hygiene

| Gap | Location | Severity |
|-----|----------|----------|
| Bulk username/photo does not skip accounts in per-account cooldown | `src/dashboard/routes.py` | Medium |
| Manager does not persist username_last_changed_at, profile_photo_last_changed_at | `src/clients/manager.py` | Medium – Per-account cooldown requires these or risk_events |
| No explicit "do not post immediately after import" guard beyond warmup | – | Low – Warmup covers |

### 5. Dashboard Honesty

| Status | Note |
|--------|------|
| ✓ | General Healthy, Session Available, Story Eligible Now labels updated |
| ✓ | Warmup Hold, Story Frozen used |
| ✓ | Alive tooltip states "not enough for stories" |
| ⚠ | get_story_availability is_story_ready can still diverge from safety policy (attempt cap, post TTL) |

### 6. Tests

| Gap | Location |
|-----|----------|
| No test that get_story_availability matches get_story_safety_decision on attempt_cap | `tests/test_safety_policy.py` or new |

---

## Files Changed

1. `src/core/session_paths.py` – Add attempt_cap, use post TTL for is_story_ready
2. `src/core/risk_events.py` – Add EVENT_MANUAL_REVIEW_CLEARED, count_events_for_account_last_hours
3. `src/dashboard/routes.py` – PATCH manual_review_required with audit; bulk username/photo per-account cooldown filter
4. `src/clients/manager.py` – Set username_last_changed_at, profile_photo_last_changed_at on success
5. `tests/test_safety_policy.py` – Test get_story_availability attempt_cap consistency

---

## Exact Patches

### Patch 1: session_paths.py – attempt_cap + post TTL

```python
# In get_story_availability, after daily_cap_blocked block (~line 212):
    attempt_cap_blocked = False
    try:
        from src.core.warmup import is_attempt_cap_exceeded
        attempt_cap_blocked = is_attempt_cap_exceeded(account)
    except Exception:
        pass

# Change precheck TTL for is_story_ready: use post TTL so UI matches execution
# Replace precheck_ttl_min = 1440 with:
    precheck_ttl_min = 1440  # display
    precheck_ttl_post = 15
    try:
        from config.settings import settings
        w = getattr(settings, "warmup", None)
        precheck_ttl_min = getattr(w, "precheck_ttl_minutes", 1440) or 1440
        precheck_ttl_post = getattr(w, "precheck_ttl_post_minutes", 15) or 15
    except Exception:
        pass
# For is_story_ready: use precheck_ttl_post (stricter)
    precheck_stale_for_ready = precheck_checked_at is None or ((now - precheck_checked_at).total_seconds() > (precheck_ttl_post * 60))

# In is_story_ready:
    and not attempt_cap_blocked
# And when precheck == "allowed": use precheck_stale_for_ready for effective_story_status_ok
```

### Patch 2: risk_events.py – EVENT_MANUAL_REVIEW_CLEARED + count per account

```python
EVENT_MANUAL_REVIEW_CLEARED = "manual_review_cleared"

def count_events_for_account_last_hours(account_id: int, event_type: str, hours: float) -> int:
    """Count events for one account in last N hours."""
    try:
        cutoff = (datetime.utcnow() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT COUNT(*) FROM account_risk_events "
                     "WHERE account_id = :aid AND event_type = :et AND created_at > :cutoff"),
                {"aid": account_id, "et": event_type, "cutoff": cutoff},
            ).fetchone()
            return row[0] if row else 0
    except Exception:
        return 0
```

### Patch 3: routes.py – PATCH manual_review_required

```python
# In update_account (PATCH), add:
        if "manual_review_required" in data:
            val = data["manual_review_required"]
            if val is False or val == 0:
                reason = (data.get("manual_review_reason") or "").strip() or "cleared_via_api"
                if not reason or len(reason) < 3:
                    return jsonify({"error": "manual_review_reason required when clearing (min 3 chars)"}), 400
                account.manual_review_required = False
                account.manual_review_reason = None
                from src.core.risk_events import record_risk_event, EVENT_MANUAL_REVIEW_CLEARED
                record_risk_event(account_id, EVENT_MANUAL_REVIEW_CLEARED, f"reason:{reason[:200]}")
```

### Patch 4: routes.py – bulk username/photo per-account cooldown

```python
# In bulk_set_username, after accounts = [a for a in accounts if account_has_canonical_session(a)]:
    from src.core.risk_events import count_events_for_account_last_hours
    cooldown_h = getattr(settings.warmup, "username_change_cooldown_hours", 6) or 6
    elig = []
    for a in accounts:
        n = count_events_for_account_last_hours(a.id, EVENT_USERNAME_CHANGED, cooldown_h)
        if n == 0:
            elig.append(a)
    accounts = elig[:max_batch] if elig else []

# Similar for bulk_set_profile_photo with profile_photo_change_cooldown_hours and EVENT_PROFILE_PHOTO_CHANGED
```

### Patch 5: manager.py – persist last_changed timestamps

```python
# In set_account_username, after acc.username = username:
                    if hasattr(acc, "username_last_changed_at"):
                        acc.username_last_changed_at = datetime.utcnow()

# In set_account_profile_photo, after success, update account:
            with get_db_context() as db:
                acc = db.query(Account).filter(Account.id == account_id).first()
                if acc and hasattr(acc, "profile_photo_last_changed_at"):
                    acc.profile_photo_last_changed_at = datetime.utcnow()
                    db.commit()
```

---

## Migrations

None. Uses existing columns and risk_events table.

---

## Restart Commands

```bash
sudo systemctl restart autostory-web
sudo systemctl restart storyfleet-scheduler
```

---

## Verification Commands

```bash
# Run safety tests
pytest tests/test_safety_policy.py -v

# Verify get_story_availability uses attempt_cap (after patch)
grep -n "is_attempt_cap_exceeded\|attempt_cap_blocked" src/core/session_paths.py

# Verify manual_review clear logs to risk_events
grep -n "EVENT_MANUAL_REVIEW_CLEARED\|manual_review_reason" src/dashboard/routes.py
```
