# Viewing STORYFLEET Logs

## Dashboard (storyfleet-dashboard service)

```bash
# Last 100 lines, follow in real time
sudo journalctl -u storyfleet-dashboard -f -n 100

# Last hour with full output
sudo journalctl -u storyfleet-dashboard --since "1 hour ago" --no-pager

# Only errors (includes Python tracebacks from 500 errors)
sudo journalctl -u storyfleet-dashboard -p err -n 50

# If dashboard writes to files instead:
# tail -f /var/log/storyfleet/dashboard-error.log
```

## Bot (storyfleet-bot service)

```bash
# Live logs
sudo journalctl -u storyfleet-bot -f -n 100

# Or from file if configured:
tail -f /var/log/storyfleet/bot-error.log
```

## Gunicorn config

The dashboard uses `deploy/gunicorn.conf.py` when run via:
```bash
gunicorn -c deploy/gunicorn.conf.py "src.dashboard.app:create_app()"
```

500 errors are logged with full Python tracebacks to stderr, which journalctl captures.
