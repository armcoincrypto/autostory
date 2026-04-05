# Operator Reference: Account States

## 1. Root cause

- **Account readiness:** `health_status=alive` was treated as "account can publish stories." In reality it only means the Telegram API responds. Many accounts are `alive` but have **no session file** on disk. Those must be re-imported. Story-ready requires session file + active + story_status ok + not blocked.
- **404 on session-audit:** The route exists at `@api.route('/accounts/session-audit')` in `routes.py`. A 404 almost always means **deployed server has old code**. Deploy latest and restart `autostory-web.service`.

---

## 2. Files changed

| File | Change |
|------|--------|
| `src/clients/manager.py` | Docstring; session_valid in results; alive ≠ story-ready |
| `src/dashboard/routes.py` | Docstrings: health check scope |
| `src/dashboard/templates/accounts.html` | Healthcheck help; session_valid column; General/Session/Story distinctions |
| `src/dashboard/templates/stories.html` | Tooltips: alive ≠ story-ready |
| `docs/OPERATOR_ACCOUNT_STATES.md` | §§ What healthcheck tests, Story-ready requires |

---

## 3. Restart commands

```bash
sudo systemctl restart autostory-web.service
```

If using scheduler (Storyfleet):

```bash
sudo systemctl restart storyfleet-scheduler.service
```

---

## 4. Session audit (curl)

```bash
curl -s http://127.0.0.1:8000/api/accounts/session-audit | python3 -m json.tool
```

If behind nginx on a domain:

```bash
curl -s https://your-domain.am/api/accounts/session-audit | python3 -m json.tool
```

---

## 5. SQL: General healthy

Accounts with `health_status=alive` or `NULL` (basic Telegram health only; does not imply session or story-ready):

```sql
SELECT id, phone_number, status, health_status, session_path
FROM accounts
WHERE status = 'active' OR health_status = 'alive' OR health_status IS NULL
ORDER BY id;
```

---

## 6. SQL: Needs re-import

No `session_path` set — must re-import via tdata:

```sql
SELECT id, phone_number, status, health_status, story_status
FROM accounts
WHERE (session_path IS NULL OR session_path = '')
ORDER BY id;
```

---

## 7. SQL: Story-ready (DB only)

DB-level filters; session file existence must be verified separately (see §8):

```sql
SELECT a.id, a.phone_number, a.session_path, a.story_status, a.story_blocked_until
FROM accounts a
WHERE a.status = 'active'
  AND (a.health_status = 'alive' OR a.health_status IS NULL)
  AND (a.session_path IS NOT NULL AND a.session_path != '')
  AND (a.story_status IS NULL OR a.story_status IN ('ok', 'unknown'))
  AND (a.story_blocked_until IS NULL OR a.story_blocked_until <= datetime('now'))
ORDER BY a.id;
```

---

## 8. Shell: Verify canonical session files

```bash
SESSIONS_DIR="/opt/autostory/data/sessions"
DB="/opt/autostory/data/storyfleet.db"

for id in $(sqlite3 "$DB" "
  SELECT id FROM accounts
  WHERE status='active' AND (health_status='alive' OR health_status IS NULL)
    AND session_path IS NOT NULL AND session_path != ''
    AND (story_status IS NULL OR story_status IN ('ok','unknown'))
    AND (story_blocked_until IS NULL OR story_blocked_until <= datetime('now'));
"); do
  if [ -f "$SESSIONS_DIR/account_${id}.session" ]; then
    echo "OK $id"
  else
    echo "MISSING $id"
  fi
done
```

---

## 9. What current healthcheck tests

The healthcheck (POST /api/accounts/check, POST /api/accounts/healthcheck) runs:

- `connect()` — connect to Telegram
- `is_user_authorized()` — session valid for API
- `get_me()` — fetch current user
- Check `User.deleted` and `User.restricted` flags
- Handle exceptions: FloodWait, UserDeactivatedBanError, UserDeactivatedError, UserRestrictedError, AuthKeyError, etc.

Result `alive` = general healthy. Result `session_valid: true` = alive + canonical session file was used.

---

## 10. What healthcheck does NOT test

- SendStoryRequest / story publishing ability
- Story cooldown (STORIES_TOO_MUCH, STORY_SEND_FLOOD)
- Story frozen/restricted by Telegram
- Any story-specific capability

**alive ≠ story-ready.** Story readiness is only proven when a story publish succeeds or when story_status was set by a prior story check.

---

## 11. What story-ready requires

1. Canonical session file exists on disk
2. `status=active`
3. `health_status` in (alive, null)
4. `story_status` in (ok, unknown) — not frozen, not restricted
5. `story_blocked_until` is null or expired
6. `purpose` allows autostory (autostory or both)

Note: story_status is updated when batch publishes or explicit story checks run. Healthcheck does not set story_status.

---

## 12. Short explanations for operators

| State | Explanation |
|-------|-------------|
| **General healthy** | `health_status=alive` — connect+auth+get_me passed. Does NOT mean session valid or story-ready. |
| **Session valid** | Canonical file exists + connect+auth+get_me passed (session_valid in healthcheck results). |
| **Session ready** | Session file exists on disk. Required for any client use (stories, scheduler, discovery). |
| **Story ready** | Session valid + active + story_status ok/unknown + not blocked. Proven when story publish succeeds. |
| **Needs re-import** | No session file — re-import via tdata (zip or session string) from Add Account modal. |

---

## 13. CLI audit script

```bash
cd /opt/autostory
python -m scripts.audit_sessions
python -m scripts.audit_sessions --json
python -m scripts.audit_sessions --db /opt/autostory/data/storyfleet.db --sessions-dir /opt/autostory/data/sessions
```

Output: per-account id, phone, health_status, session_path, canonical_exists, story_status, classification, action (ok|wait|reimport).
