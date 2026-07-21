# Telegram Session Migration Rehearsal

## Tool

`scripts/db/rehearse_telegram_session_encryption.py`

The utility is dry-run by default, accepts only an explicit database path, supports protected live paths, requires an exact plaintext-row precondition for controlled execution, and requires both `--apply` and `--acknowledge-disposable-copy`.

## Result on repaired copy

- Session-bearing account rows: 104
- Plaintext rows before: 104
- Already encrypted rows before: 0
- Migrated rows: 104
- Plaintext rows after: 0
- Per-row authenticated decrypt/hash verification: passed
- Second migration: 0 rows (idempotent)
- ORM reads through `EncryptedSessionText`: 104 successful plaintext-in-memory values
- External Telegram connections: 0
- Production rows migrated: no

The rehearsal key was generated only for the isolated process environment and was not printed, committed, or placed in an audit document. The resulting copy is not a production backup and cannot be promoted.

## Focused tests

Tests cover legacy plaintext reads, encrypted writes, authenticated round-trip, tampering, wrong/missing key, key ID retention, encrypted-only rejection, centralized SQLAlchemy conversion, mixed-format migration, idempotency, and protected-target refusal.

## Production proposal

Do not run until the Telegram path map is reviewed, a durable versioned key is provisioned, all active writer processes use the new model type, immutable release lineage is restored, a consistent backup is captured, and writer concurrency is controlled. Batch/progress semantics can be added if row count or lock-time analysis requires it; the current 104-row rehearsal completed atomically.
