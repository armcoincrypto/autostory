# Rollback and Recovery

| RELEASE | READS_PLAINTEXT | READS_ENCRYPTED_V1 | WRITES_PLAINTEXT | WRITES_ENCRYPTED_V1 | SAFE_AFTER_MIGRATION |
|---|---|---|---|---|---|
| c9d1fe6 (live) | yes | only if keys+code present | yes (disabled) | no | NO without DB restore if rows encrypted without compatible readers |
| Phase 0.8 branch | yes | yes with keys | disabled→plain; transition→enc | transition/encrypted-only | YES with keys |

## Tested on rehearsal
- Database restore from pre-migrate copy → plaintext 104, encrypted 0, integrity ok
- Key restore: reload env file
- Application rollback to pre-encryption release after encrypt without DB restore: **unsafe**

Critical: code rollback without encrypted-session support requires simultaneous DB restore.
