# Phase 1.1 Final Report

## Final verdict

```text
ZOLLOTEX_SOCIAL_AGENT_PHASE1_1_PARTIAL_ROOT_CAUSE_CLOSED_MUTATION_BOUNDARY_DEPLOYED_OBS_CONTAMINATED_BY_AUTHORIZED_106_CANARY
```

## Starting identity

* Production SHA/release (phase start): `a28b54ab2c2f` / `20260722T140147Z-a28b54ab2c2f`
* Web/scheduler/readiness: unified `a28b54a`
* Session mode: `encrypted-only` / `prod-v1`
* Safety flags: CONTROLLED=false, SCHEDULER_MUTATIONS=false, AI/gateway/bot/Kathleen off
* Legacy: quarantine 104 / backups 511

## Incident event (original)

* Timestamp: `2026-07-22T14:04:35.513410Z` (DB published_at ≈ Telegram accept)
* Account: 140
* Story ID (DB): 29 / External Telegram story_id: 2
* Candidate: n/a (0 mentions)
* Trigger: Phase 0.6 controlled canary `POST /api/stories/runs`
* Status: story_run 9 completed, stories_ok=1

## Timeline (summary, UTC)

1. ~13:56 — Phase 0.6 account-140 canary evidence opened
2. 14:01–14:04 — a28b54a promote + web restart storm
3. Temporary drop-in `CONTROLLED_STORY_EXECUTION_ENABLED=true` + ACCOUNT_ID=140
4. Attempt1 403 (drop-in order); Attempt2 publish success PID 684742
5. Flags restored false; later encrypted-only override rewrite ~14:12
6. ~19:20 — Phase 1.1 `e2f11a0` deployed (boundary hardening)
7. ~20:18–20:19 — Concurrent Phase 0.6 account-106 canary on `c862d9f` enabled full flag stack; Story 30 published
8. Flags restored false; observation T60 saw stories=9

## Responsible execution path (original incident)

```text
SERVICE=autostory-web
PID=684742
RELEASE_SHA=a28b54ab2c2f3f9caed29614f14add4b26a19337
ENTRY_POINT=POST /api/stories/runs
FUNCTION=src.stories.publisher.StoryPublisher.publish_story → SendStoryRequest
TRIGGER=phase0-6_account_140_controlled_canary
CONFIG_SOURCE=temporary systemd drop-in (CONTROLLED_STORY_*=true) then removed
```

## Root cause

* Primary category: `CANARY_PATH_BYPASS` / concurrent authorized controlled-live canary
* Exact defect: `CONTROLLED_STORY_EXECUTION_ENABLED=true` alone authorized live publish; no independent global kill switch / provider token
* Why flags “failed”: post-hoc reports sampled cleanup state; execution-time process had CONTROLLED=true (`controlled_story_publish_ok`)
* Why monitoring did not prevent: controlled canary is designed to publish when purpose flag is on
* Recurrence scope: any operator able to toggle CONTROLLED (pre-1.1) or the full 1.1 stack (post-1.1) can publish; accidental CONTROLLED-only recurrence is blocked after 1.1

## Contributing factors

* Concurrent phases without deploy/mutation freeze
* Post-cleanup config sampling
* Systemd drop-in lexical ordering
* Direct gunicorn POST (no nginx POST log)
* No provider authorization token (pre-1.1)

## Mutation path inventory

* Total documented paths: 19 (incl. non-publish)
* Production reachable live-capable: 1 (P1 controlled live) when fully authorized
* Direct provider bypasses before: dual publishers callable without provider token
* Direct provider bypasses after: 0 (`invoke_send_story` only)
* Unknown paths: 0

## Hardening delivered

* Global kill switch `STORY_MUTATIONS_ENABLED` (default deny)
* Execution mode `STORY_EXECUTION_MODE`
* Account allowlist `STORY_ACCOUNT_MUTATION_ALLOWLIST` (empty deny)
* Trigger authorization (scheduler/automation/recovery denied)
* Provider boundary HMAC single-use tokens via `StoryMutationService` / `invoke_send_story`
* Idempotency / single-use + invalidate-on-disable
* Queue recheck at consume time
* Structured denial audit logs

## Tests

* Full regression: **709 passed**, 13 skipped
* Focused security tests: **PASS ×2** (`test_story_mutation_boundary_phase1_1`)
* Incident reproduction: CONTROLLED=false / missing global → PROVIDER_CALL_COUNT=0
* Phase 0.6 pattern: CONTROLLED=true but STORY_MUTATIONS=false → DENY
* Dry-run: zero provider calls
* Queue/retry after disable: DENY at consume
* Encryption regression: maintained in runtime checks
* Syntax/hygiene: suite green

## Deployment

* Branch: `security/autostory-phase1-1-story-mutation-boundary-20260722`
* SHA (Phase 1.1 harden): `e2f11a0b1c5d4aad205015f4247742872c58ea91`
* Remote: pushed
* Release: `/opt/autostory-releases/20260722T192010Z-e2f11a0b1c5d`
* Previous: `/opt/autostory-releases/20260722T140147Z-a28b54ab2c2f`
* Services changed: web, scheduler, readiness
* Later concurrent release (not this phase’s deploy): `c862d9f8f8c0` (includes e2f11a0 as ancestor + account-scoped controlled live)
* Rollback target: `20260722T140147Z-a28b54ab2c2f` (pre-boundary) or re-pin e2f11a0 release

## Final runtime identity (at report time)

```text
WEB_SHA=c862d9f8f8c06bad64e390bd3ca43c06ab93a467
SCHEDULER_SHA=c862d9f8f8c06bad64e390bd3ca43c06ab93a467
READINESS_SHA=c862d9f8f8c06bad64e390bd3ca43c06ab93a467
```

(Release path: `/opt/autostory-releases/20260722T201852Z-c862d9f8f8c0`)

## Production denial validation (post-cleanup, current)

| ENTRY_POINT | DECISION | DENIAL_REASON | PROVIDER_CALLED | EXTERNAL_ID |
|---|---|---|---|---|
| POST /api/stories/runs (140) | deny | story_mutations_disabled | false | null |
| POST /api/stories/runs (106) | deny | story_mutations_disabled | false | null |
| POST /api/stories/publish | deny | story_execution_disabled (p3) | false | null |
| POST /api/stories/batch | deny | story_execution_disabled (p3) | false | null |

## Observation

| Checkpoint | UTC | stories | enc/plain | services | publish_hits_since_1.1_deploy |
|---|---|---|---|---|---|
| T+0 | 19:21:25Z | 8 | 104/0 | active | 0 |
| T+5 | 19:26:26Z | 8 | 104/0 | active | 0 |
| T+15 | 19:36:26Z | 8 | 104/0 | active | 0 |
| T+30 | 19:51:26Z | 8 | 104/0 | active | 0 |
| T+60 | 20:21:27Z | **9** | 104/0 | active on c862d9f | **1** |

Contamination: authorized account-106 canary at `2026-07-22T20:19:37Z` (story DB id 30, run 10), after enabling full 1.1 flag stack via `zzz-controlled-canary-106.conf`.

## Safety invariants (current)

```text
STORY_MUTATIONS_ENABLED=false
SCHEDULER_MUTATION_LOCK=true (SCHEDULER_MUTATIONS_ENABLED=false)
AI_LOOP_STATE=disabled
GATEWAY_STATE=inactive
BOT_STATE=inactive
KATHLEEN_STATE=inactive
SESSION_ENCRYPTION_MODE=encrypted-only
SOCIAL_POSTS_CREATED=1 during observation window (authorized 106 canary; not Phase 1.1 action)
MESSAGES_SENT=0
FUNDS_MOVED=0
```

## Evidence retention

* Quarantined sessions: 104
* Backup sessions: 511
* Runtime dependency: 0
* Evidence hold: true
* Deletion status: not allowed

## Production mutations (this phase)

1. Deploy e2f11a0 release + systemd flags including STORY_MUTATIONS_ENABLED=false
2. Readiness override fix (READINESS_WORKER_ENABLED=true)
3. Denial smokes only (no publish)
4. External concurrent: c862d9f deploy + account-106 canary (other workstream)

## Decision gates

| Gate | Result |
|---|---|
| A Incident attribution | **PASS** |
| B Path inventory | **PASS** |
| C Canonical boundary | **PASS** |
| D Fail-closed controls | **PASS** |
| E Queue/retry safety | **PASS** |
| F Zero external mutation during phase | **FAIL** (authorized 106 canary) |
| G Incident closure (full) | **PARTIAL** — original incident closed; recurrence of CONTROLLED-only blocked; concurrent full-stack canary still possible by design |

## Social Agent decision matrix

```text
FOUNDATION_DEVELOPMENT_ALLOWED=true (isolated only)
PRODUCTION_DEPLOYMENT_ALLOWED=false
EXSWAPING_CONTENT_TOOLS_ALLOWED=false
SOCIAL_ACCOUNT_CONNECTION_ALLOWED=false
LIVE_PUBLISHING_ALLOWED=false
FK_PRODUCTION_MIGRATION_ALLOWED=false
TELEGRAM_PRODUCTION_MIGRATION_ALLOWED=false
TELEGRAM_ENCRYPTED_ONLY_ALLOWED=true
LEGACY_SESSION_DELETION_ALLOWED=false
```

## Remaining blockers

1. **Owner: program coordination** — enforce deploy/canary freeze across concurrent Phase 0.6 workstreams during observation windows; optional hard “canary approval record” persisted before flag enable.
2. **Owner: security** — consider requiring dual-control / time-boxed canary lease in DB so systemd drop-ins alone cannot enable live mode.
3. Legacy session secure deletion still deferred (evidence hold).

## Next priority

`legacy session secure deletion` is not yet allowed. Choose:

**remaining credential rotation** or **Swaperex endpoint hardening** or **origin TLS repair** — bounded infra work that does not require live Story publishing.

Recommended next: **Swaperex endpoint hardening** (or origin TLS repair) while Social Agent live publishing remains denied and legacy sessions stay on hold until a dedicated deletion phase after canary coordination is locked.
