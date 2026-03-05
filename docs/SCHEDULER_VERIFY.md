# Scheduler Health Check Commands

## 1. Restart services

```bash
sudo systemctl restart storyfleet-dashboard
sudo systemctl restart storyfleet-bot      # Telegram bot
sudo systemctl restart storyfleet-scheduler # Auto-send at random times
```

## 1b. Enable and start scheduler (first time)

```bash
sudo cp deploy/storyfleet-scheduler.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable storyfleet-scheduler
sudo systemctl start storyfleet-scheduler
```

## 2. Check service status

```bash
sudo systemctl status storyfleet-dashboard
```

## 3. View logs

```bash
# Dashboard (Gunicorn) logs
sudo journalctl -u storyfleet-dashboard -f -n 100

# Or recent lines
sudo journalctl -u storyfleet-dashboard --since "10 min ago" --no-pager

# Errors only
sudo journalctl -u storyfleet-dashboard -p err -n 50
```

## 4. Trigger test send (after Save in UI)

```bash
cd /opt/autostory
source venv/bin/activate

# Replace account_id and target_id with actual IDs from your bindings
curl -sS -X POST http://127.0.0.1:5000/api/v1/jobs/run-now \
  -H "Content-Type: application/json" \
  -d '{"account_id": 1, "type": "PROMO", "target_id": 1}' | jq .
```

Expected success: `{"success": true, "job_id": 123}`
Expected failure: `{"error": "...", "error_code": "...", "job_id": 123}`

## 5. Inspect database (SQLite)

The default DB is `data/storyfleet.db` (or check DATABASE_URL in .env).

```bash
cd /opt/autostory
# Use the DB your app uses (from .env DATABASE_URL or default)
sqlite3 data/storyfleet.db
```

```sql
-- Last 20 deliveries with errors
.headers on
.mode column
SELECT id, account_id, target_id, type, status, error_code, substr(error_message,1,80) AS err, sent_at, created_at
FROM message_deliveries
ORDER BY id DESC
LIMIT 20;

-- Last 20 scheduled jobs
SELECT id, account_id, target_id, type, run_at, status, substr(last_error,1,80) AS last_error
FROM scheduled_jobs
ORDER BY id DESC
LIMIT 20;

-- Bindings (account-target pairs)
SELECT * FROM account_target_bindings;

-- Templates for account
SELECT id, type, scope, account_id, substr(body,1,50) FROM message_templates WHERE scope='ACCOUNT';
```

## 6. Verify Save created data

After clicking "Save settings" in the UI, confirm:

```sql
-- Bindings exist for your account
SELECT b.id, b.account_id, b.target_id, b.can_post, b.allowed_types
FROM account_target_bindings b
WHERE b.account_id = <YOUR_ACCOUNT_ID>;

-- Template exists
SELECT id, type, scope, account_id, substr(body,1,100)
FROM message_templates
WHERE account_id = <YOUR_ACCOUNT_ID> AND scope = 'ACCOUNT';

-- Profile exists
SELECT * FROM schedule_profiles WHERE account_id = <YOUR_ACCOUNT_ID>;

-- Rule exists
SELECT id, type, times_json, target_mode, selected_target_ids_json
FROM schedule_rules
WHERE account_id = <YOUR_ACCOUNT_ID>;
```
