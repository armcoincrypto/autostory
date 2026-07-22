# Session Storage Inventory

## Database
| PATH_OR_COLUMN | FORMAT | CLASS | CONTAINS_PLAINTEXT | MIGRATION_REQUIRED |
|---|---|---|---|---|
| accounts.session_string | path/string/envelope | CANONICAL_DATABASE_STORAGE | yes (prod) | yes |
| accounts.session_path | filesystem path text | CANONICAL_DATABASE_STORAGE (pointer) | path only | clear after materialize |

Live counts: accounts=104, session_string NN=104 (102 path-like, 2 opaque), enc:v1=0, session_path NN=102.

## Filesystem
| PATH | CLASS | COUNT | OUTCOME |
|---|---|---:|---|
| /opt/autostory/data/sessions/*.session | ACTIVE_FILESYSTEM_STORAGE | 104 | IMPORT_TO_CANONICAL_DATABASE (rehearsed) |
| /opt/autostory/data/backups/**/*.session | BACKUP_ONLY | 511 | RETAIN_READ_ONLY_DURING_TRANSITION |
| /opt/autostory/data/bot/*.session | ACTIVE_FILESYSTEM_STORAGE (bot) | 1 | BLOCKED_OWNER_DECISION (bot rail separate) |
| /opt/autostory/data/recovery_lab/** | RECOVERY_TOOL | 10 .session | FORMALLY_RETIRE when unused |
| /opt/autostory/data/session_imports/** | EXPORT_IMPORT_PATH | 2 .session | FORMALLY_RETIRE |
| data/sessions/.key | LEGACY_FILESYSTEM_STORAGE (Fernet) | 1 | FORMALLY_RETIRE (gated) |

## Cache / Redis
Legacy Fernet Redis `session:<id>` in `core/session_manager.py` — CACHE_ONLY / OBSOLETE; runtime gated off unless `LEGACY_FERNET_SESSION_MANAGER_ENABLED=true`.

## Unknown
None remaining after inventory.
