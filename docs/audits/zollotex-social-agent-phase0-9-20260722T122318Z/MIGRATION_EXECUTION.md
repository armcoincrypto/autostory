# Migration Execution

| Step | Result |
|---|---|
| Dry-run would_migrate | 104 |
| Blocked / unknown | 0 / 0 |
| Migrated | 104 |
| Failed | 0 |
| Duration | ~2s |
| Idempotent rerun migrated | 0 |
| Materialize filesystem | yes |
| Lock | session-migration.lock held |
| Mode | transition |
| Key ID | prod-v1 |
| Command | migrate_telegram_session_encryption.py migrate --allow-production --acknowledge-production-migration --materialize-filesystem-sessions |
