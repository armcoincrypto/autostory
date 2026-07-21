# Missing Module Root Cause

Phase 0.5 reproduced 18 collection errors. Clean `6858591` reduced these to 11 but still lacked
runtime source. The root cause was uncommitted production development: tracked importers referred
to files present only in the dirty runtime.

Resolved classes:

- `CANONICALIZE_RUNTIME_FILE`: readiness stores/workers, target health, account operational and
  runtime state, UTC helpers, session lock v2, Telethon schema, governance/readiness packages.
- `RESTORE_FROM_EXISTING_COMMIT`: Story scheduler integration and current web release fixes from
  `6858591`, `491ba85`, and `499758e`.
- `REMOVE_STALE_IMPORT`: governance and readiness helpers no longer import recovery modules only
  for constants or hashing.
- `UPDATE_OBSOLETE_TEST`: protection constants now come from `src.core.account_protection`;
  legacy placeholder batch tests were removed.
- `BLOCKED_UNKNOWN_IMPLEMENTATION`: Kathleen wrappers require ignored bytecode and were rejected;
  the placeholder `src/stories/run_batch.py` was rejected.

Result: test collection succeeds with no missing-module error and no placeholder or global skip.
The later functional regression failures are not collection failures and are separately retained
as blockers.
