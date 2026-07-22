# Trigger Source Analysis

## Responsible trigger

`MANUAL_OPERATOR_EXECUTION` / concurrent **Phase 0.6 Account 140 Controlled Canary**.

Not scheduler, not readiness, not AI loop, not gateway/bot/Kathleen, not celery.

## Sequence (UTC)

1. ~13:56Z — Phase 0.6 canary evidence dir created
2. 14:01–14:04Z — release `a28b54a` promote + multiple web restarts
3. Temporary drop-in: `CONTROLLED_STORY_EXECUTION_ENABLED=true`, `CONTROLLED_STORY_ACCOUNT_ID=140`
4. Attempt 1: HTTP 403 `controlled_story_execution_disabled` (drop-in lexical order; no Telegram)
5. Attempt 2: HTTP 200 — Telegram publish story_id=2 / db 29 / run 9
6. Drop-in removed; web restarted; flags false again
7. 14:12Z — Phase 1.0 encrypted-only override rewrite

## Config truth at execution

| Control | Expected (post-hoc report) | Process 684742 loaded |
|---|---|---|
| CONTROLLED_STORY_EXECUTION_ENABLED | false | **true** (temporary drop-in) |
| CONTROLLED_STORY_ACCOUNT_ID | (unset/deny) | **140** |
| STORY_MUTATIONS_ENABLED | (did not exist) | n/a |
| SCHEDULER_MUTATIONS_ENABLED | false | false (irrelevant to this path) |
