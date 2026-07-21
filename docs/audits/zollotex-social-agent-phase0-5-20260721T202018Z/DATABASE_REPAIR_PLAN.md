# Database Repair Plan

## Tool

`scripts/db/rehearse_fk_repair.py`

The tool is dry-run by default and requires an exact violation-count precondition. Mutation additionally requires `--apply`, `--acknowledge-disposable-copy`, and a target that does not match any `--protected-path`.

## Deterministic operation

1. Enable foreign-key enforcement for the repair connection.
2. Assert the exact expected violation count.
3. Refuse violating tables outside the reviewed six-table allowlist.
4. Create `phase05_fk_orphan_archive` inside the copied database.
5. Archive each violating row once with source table, source row ID, full row JSON, reason, and timestamp.
6. Delete only the archived orphan rows.
7. Run `PRAGMA foreign_key_check` before commit; roll back if any violation remains.
8. Commit, then run structural and aggregate checks.

The migration is transactional, count-guarded, narrowly scoped, and idempotent. Full archived row payloads remain only in the restricted database copy and are never placed in Git or audit Markdown.

## Proposed production boundary

Production execution is **not approved**. Before any future execution:

- take another consistent backup and validate its hash;
- stop or quiesce all writers under an approved maintenance plan;
- ensure the expected count/families still match;
- confirm archive retention and access ownership;
- run with the live database supplied as a protected path during dry-run;
- obtain data-owner approval for archival/deletion;
- prepare restore-from-backup rollback;
- verify the immutable release can boot before touching data.
