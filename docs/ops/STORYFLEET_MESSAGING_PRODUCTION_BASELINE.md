# Storyfleet Telegram Messaging — Production Baseline

Frozen after final closure. Do not treat this as a feature backlog.

## Runtime

```text
Canonical current: /opt/autostory-releases/current
Compat alias:      /opt/autostory-current
DB:                /opt/autostory/data/storyfleet.db (SQLite WAL)
Deploy:            /usr/local/sbin/storyfleet_deploy_production.sh --sha <sha>
Rollback:          code-only via storyfleet_rollback_production.sh <release-path>
```

`/opt/autostory/current` is not a referenced pointer. Do not recreate it unless a consumer appears.

## Owners

```text
JOIN     = OwnerChatService → join_ref_for_account
SEND     = OwnerDirectMessageService
SCHEDULE = ScheduledDirectMessageService → OwnerDirectMessageService
HISTORY  = TelegramDmTransport
CATALOG  = chat_catalog (DB-derived)
```

Do not add a second send, join, or schedule owner.

## Owner workflows

- Single account: search, chats, history, AI Draft (review-only), Send Now, Schedule, cancel, join.
- Multiple accounts: catalog, readiness matrix, bounded join, spaced schedule, bulk cancel.
- No multi-account Send Now.

## Normal flags

```text
MESSAGES_EXECUTION_ENABLED=true
MESSAGES_CHAT_JOIN_ENABLED=true
MESSAGES_GROUP_CHANNEL_SEND_ENABLED=true
SCHEDULED_DM_ENABLED=true
MESSAGES_CHAT_LEAVE_ENABLED=false
SCHEDULER_MUTATIONS_ENABLED=false
SCHEDULER_PROMO_MUTATIONS_ENABLED=false
SCHEDULER_INFO_MUTATIONS_ENABLED=false
DISCOVERY_EXECUTION_ENABLED=false
BROADCAST_EXECUTION_ENABLED=false
```

Intentionally disabled: leave, legacy scheduler promo/info mutations, Discovery execution, Broadcast execution.

## Safety rules

- Bulk schedule requires `idempotency_key`. Missing key → 422. Same key + different payload → 409. Replay does not create duplicate jobs.
- UNCERTAIN is never auto-retried. Check the chat before a new send.
- Scheduled group/channel sends recheck membership and write permission before send. Private path is unchanged.
- Overdue scheduled DMs older than 15 minutes fail closed (`OVERDUE_SKIPPED`). No burst after outage.
- Cancel is atomic and PENDING-only. RUNNING is not reported as Cancelled.
- Telegram service peer `777000` is excluded from catalog/dialogs; history codes are masked; Send, Schedule, Join, and AI Draft are blocked. Nothing from that chat is sent to the AI provider.
- Slow mode maps to owner copy (`This chat has slow mode enabled…`). No automatic retry.
- Protected and reserved accounts cannot join, send, or schedule.

## Health

- Web, scheduler, readiness worker must be active.
- Ops health timer and host monitor (`server-health-monitor`) plus journald are the alert path. No separate Telegram/email alert channel is required for normal operation while the host monitor is watched.
- Backups: daily encrypted off-host (`storyfleet-offhost-backup.timer`).
- Release retention: keep newest 7 immutable releases plus current/previous/active systemd targets (`storyfleet_retention_maintenance.sh`).

## Deploy gates

Refuse cutover on Python syntax failure, Messages JS syntax failure (`node --check`), unfetchable SHA, or secret-scan failure. Builds come from the bare deploy mirror, not a dirty worktree.
