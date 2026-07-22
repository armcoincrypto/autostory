# Migration Preflight

| Check | Result |
|---|---|
| Runtime unified | yes @ 181c473 |
| Mode | transition |
| Key | prod-v1 |
| PLAINTEXT_ROWS | 104 |
| ENCRYPTED_ROWS | 0 |
| SESSION_WRITERS_DISCOVERED | web account-import/auth, readiness session touch, manager reconnect |
| SESSION_WRITERS_PAUSED | web stopped, readiness stopped; scheduler remains mutation-locked |
| ACTIVE_SESSION_WRITES | 0 (services paused) |
| MIGRATION_LOCK_ACQUIRED | true |
