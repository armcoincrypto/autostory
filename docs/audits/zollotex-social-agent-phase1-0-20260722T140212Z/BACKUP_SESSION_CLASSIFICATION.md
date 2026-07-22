# Backup Session Classification

| Field | Value |
|---|---|
| COUNT | 511 |
| LOCATION_REDACTED | /opt/autostory/data/backups/** |
| MODE | 0600 (restricted in Phase 0.9) |
| ACTIVE_RUNTIME_DEPENDENCY | false |
| RECOVERY_VALUE | legacy FS auth copies; superseded by encrypted DB + pre-encrypt DB backup |
| EXPOSURE_RISK | medium if world-readable (now restricted) |
| RECOMMENDED_ACTION | RETAIN under quarantine policy; SECURE_DELETE_AFTER_APPROVAL later |
| DELETION_APPROVAL | blocked pending owner |
