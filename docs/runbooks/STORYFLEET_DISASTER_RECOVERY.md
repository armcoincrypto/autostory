# Storyfleet Disaster Recovery (Wave C)

## Purpose

Recover Storyfleet if the production host is lost or corrupted.

This runbook restores **data + session encryption keys + config** together.
Do **not** connect restored Telegram sessions live against production while the
original host still holds the same sessions (session contention / bans).

## What is backed up

| Asset | Location in bundle |
| --- | --- |
| SQLite DB (consistent `.backup`) | `db/storyfleet.db` |
| App env | `config/autostory.env` ← `/opt/autostory/.env` |
| Telegram session encryption keys | `keys/telegram-session-keys.env` + `keys/sessions.key` |
| Owner media uploads | `media/` |
| systemd unit definitions / overrides | `systemd/` |
| Release pointer + manifest | `release/` |

Not included (by design): the large historical `/opt/autostory/data/backups` tree,
old worktrees, venv, or live Telegram network state.

## Automation

- Script: `scripts/ops/storyfleet_offhost_backup.sh`
- Timer: `storyfleet-offhost-backup.timer` (daily ~03:15 UTC+host tz)
- Status: `/opt/autostory/data/runtime/offhost_backup_status.json`
- Age monitor: `storyfleet-backup-age-check.timer` (every 6h; fails if backup stale/failed)
- Secrets: `/etc/autostory/offsite-backup.env` (mode 0600)

Requirements encoded in status/ops:

```text
OFF_HOST=YES
ENCRYPTED=YES
AUTOMATED=YES
RETENTION=BOUNDED
LATEST_BACKUP_AGE_MONITORED=YES
```

## Manual backup now

```bash
sudo /opt/autostory-releases/current/scripts/ops/storyfleet_offhost_backup.sh
sudo /opt/autostory-releases/current/scripts/ops/storyfleet_backup_age_check.sh
```

## Restore drill (disposable / staging)

Never point live systemd units at the restore directory during a drill.

```bash
# From newest local encrypted bundle:
sudo /opt/autostory-releases/current/scripts/ops/storyfleet_offhost_restore_drill.sh

# Or pull latest off-host bundle then restore:
sudo PULL_LATEST=1 /opt/autostory-releases/current/scripts/ops/storyfleet_offhost_restore_drill.sh
```

Evidence file: `/var/tmp/storyfleet-dr-restore/<stamp>/RESTORE_EVIDENCE.txt`

Expected proofs:

1. GPG decrypt OK + sha256 match
2. `PRAGMA integrity_check` = `ok`
3. `accounts` readable; session_string rows present
4. session key files present and non-empty
5. Telegram live connect **skipped**

Dispose:

```bash
sudo rm -rf /var/tmp/storyfleet-dr-restore/<stamp>
```

## Full host recovery (outline)

1. Provision replacement host; install OS packages, Python venv, nginx as usual.
2. Restore off-host bundle to a staging path; verify integrity (restore drill).
3. Stop any leftover primary that might still hold Telethon sessions.
4. Install `/opt/autostory/.env` from `config/autostory.env`.
5. Install session keys to `/etc/autostory/telegram-session-keys.env` and
   `/opt/autostory/data/sessions/.key` (modes `0600`).
6. Place `db/storyfleet.db` at `/opt/autostory/data/storyfleet.db` (stop writers first).
7. Restore `media/` into `/opt/autostory/data/media`.
8. Deploy an immutable release (`scripts/release/deploy_production.sh --sha <known-good>`).
9. Restore systemd units if needed; `daemon-reload`; start web → readiness → scheduler.
10. Owner login + Accounts/Messages dry checks **before** any live Telegram send.

## Kill switches / safety

- Keep `SCHEDULED_DM_ENABLED=false` and `SCHEDULER_MUTATIONS_ENABLED=false` until
  fleet health is confirmed after restore.
- Prefer Messages dry-run before Send Now.
- If both old and new hosts are up, do not dual-connect the same session material.

## Install units (once per host)

```bash
sudo /opt/autostory-releases/current/scripts/ops/install_storyfleet_offhost_backup_units.sh
```
