# Rollback Readiness

| Scenario | Action |
|---|---|
| A before migration | restore prior release overrides from evidence systemd-backup |
| B key installed, plaintext DB | remove key EnvironmentFile; keep release or roll back |
| C after migration | restore DB backup `storyfleet.pre-encrypt.20260722T124751Z.db` + prior release; OR stay on transition release and fix forward |
| D key unavailable | restore `/etc/autostory/telegram-session-keys.env` from evidence backup |

```text
PREVIOUS_DATABASE_BACKUP_VALID=true
KEY_BACKUP_VALID=true
PREVIOUS_RELEASE_VALID=true
ROLLBACK_COMPATIBILITY_MATRIX_COMPLETE=true
```

Do not deploy plaintext-only release against encrypted rows without DB restore.
