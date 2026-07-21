# Canonicalization Decisions

Accepted source has understood callers, no detected secrets, source-backed imports, focused tests,
and fail-closed defaults. Commits are intentionally separated into production safety history,
runtime dependency closure, inactive AI source, scheduler/runtime safety, operator dashboard,
secret hygiene, and tests.

Rejected:

- dirty `src/dashboard/routes.py`: a wrapper for untracked `_routes_bytecode_archive.pyc`;
  candidate retains the tracked source-backed route implementation and Phase 0.5 health boundary;
- Kathleen package and listener: wrappers execute ignored CPython 3.12 bytecode;
- `src/stories/run_batch.py`: explicitly describes itself as a placeholder restore;
- environment files/backups, databases, sessions, logs, caches, generated data and runtime locks;
- recovery/phase scripts lacking active runtime necessity and complete review.

The candidate incorporates current immutable web fixes `491ba85` and `499758e`, but it is not a
production deployment package until regression and runtime-delta blockers are closed.
