# Legacy Session Retention Decision

| Group | Count | Action |
|---|---:|---|
| Quarantined phase0-9 sessions | 104 | RETAIN_UNTIL_ENCRYPTED_ONLY_SOAK_COMPLETE |
| Backup .session under data/backups | 511 | RETAIN_UNTIL_KEY_BACKUP_VALIDATED + owner deadline |
| Active runtime dependency | 0 | none |

```text
AUTOSTORY_LEGACY_SESSION_RETENTION_APPROVED
AUTOSTORY_LEGACY_SESSION_DELETION_BLOCKED_OWNER_APPROVAL
```

Deletion deferred: encrypted DB backup + key backup + encrypted-only restart must be verified; storage cannot claim forensic SSD erasure.
Owner: ops. Deadline: after encrypted-only observation soak + explicit approval.
