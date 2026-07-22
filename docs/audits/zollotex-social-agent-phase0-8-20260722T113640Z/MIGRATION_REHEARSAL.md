# Migration Rehearsal

1. Inspect: plaintext=104, encrypted=0, path-like=102, opaque=2
2. Dry-run with materialize: would_migrate=104
3. Pre-migrate backup of copy created
4. Migrate + materialize: migrated_rows=104, verified_rows=104, plaintext_after=0
5. Verify: failures=0, encrypted_v1=104
6. Idempotent rerun: migrated_rows=0
7. PRAGMA quick_check=ok; integrity_check=ok; foreign_key_check=910 (unchanged class of FK debt)
8. session_path NN after materialize on copy: 0
9. Production DB unchanged: enc=0, plain=104
