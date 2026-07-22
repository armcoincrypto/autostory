# Filesystem Session Classification

Preferred architecture: durable auth material lives as encrypted StringSession in `accounts.session_string`. Filesystem `.session` files are temporary runtime artifacts only when Telethon requires them.

## Active outcomes
- Canonical `data/sessions/account_*.session`: **IMPORT_TO_CANONICAL_DATABASE** via `--materialize-filesystem-sessions` (rehearsed on disposable DB copy).
- Production FS files were **not** deleted in Phase 0.8.
- After authorized production migration: retire active FS sessions (chmod/move/delete under runbook) only after verify + observation.

## Temporary runtime policy
If a SQLite session file must exist: restricted dir, mode 0600, remove on clean shutdown, exclude from backups/releases, crash residue cleanup documented in runbook.
