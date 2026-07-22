# Root Cause Analysis — Unauthorized Story at 2026-07-22T14:04:35Z

## Primary category

`CANARY_PATH_BYPASS` (concurrent authorized controlled-live canary)

More precisely: **intentional Phase 0.6 controlled canary** that temporarily enabled
`CONTROLLED_STORY_EXECUTION_ENABLED=true` via a systemd drop-in, published one Story,
then restored flags to false. Post-incident observation sampled the **post-cleanup**
configuration and incorrectly treated the publication as unexplained bypass of a
persistently-disabled flag.

## Exact defect

There was **no durable independent global kill switch** that remained deny while a
canary briefly flipped `CONTROLLED_STORY_EXECUTION_ENABLED`. Enabling the controlled
purpose flag alone was sufficient (together with operator confirmation token +
account 140 scope) to reach `SendStoryRequest`.

## Why reported flags failed to prevent it

Reported safety state (`CONTROLLED_STORY_EXECUTION_ENABLED=false`, scheduler lock, etc.)
described the **steady-state after canary cleanup** (and later encrypted-only rewrite
of override.conf at 16:12:28 CEST). At execution time PID `684742` loaded
`CONTROLLED_STORY_EXECUTION_ENABLED=true` (evidence: `reason_code=controlled_story_publish_ok`
and Phase 0.6 `commands.log` step 7–9).

## Why monitoring did not prevent it

Controlled live is designed to publish when its purpose flag is on. No provider-boundary
authorization token or `STORY_MUTATIONS_ENABLED` default-deny layer existed. Concurrent
Phase 0.6 and Phase 1.0 lacked a shared mutation freeze.

## Recurrence scope

Any operator/process that can:
1. write a temporary systemd drop-in enabling `CONTROLLED_STORY_*`, and
2. POST `/api/stories/runs` with `LIVE_STORY_ACCOUNT_140` + approval,

could publish again — until Phase 1.1 global kill switch + account allowlist + mode +
provider token boundary.

## Attribution evidence

- Phase 0.6 evidence: `/opt/autostory-phase0-6-evidence/account-140-controlled-canary-20260722T135652Z/`
- Journal: gunicorn PID 684742, `controlled_story_run_started` / `Story published successfully`
- DB: stories.id=29, story_runs.id=9, telegram story_id=2, account_id=140
