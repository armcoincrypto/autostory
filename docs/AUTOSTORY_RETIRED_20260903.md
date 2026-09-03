# AutoStory — Retired (2026-09-03)

Date retired: 2026-09-03
Branch: `chore/retire-autostory` (based on `main` @ `3a78a5965e9892c49091d8a1af811382019c450a`)
Status: Product retired. Telegram fleet, sessions, and every other Storyfleet/Zellotex
product are unaffected and continue operating normally.

## Why

AutoStory (the recurring-campaign Story automation product) is no longer needed.
This was a deliberate product decision, not a response to a defect — the underlying
Story-publishing capability it was built on (precheck, dry-run, controlled live run)
remains in place and is preserved as the "Manual Story" workflow. See
[AUTOSTORY_PRODUCT_POLICY_20260901.md](AUTOSTORY_PRODUCT_POLICY_20260901.md) for the
verified production evidence (Campaign #18/#20 capacity limits, the Premium-account
requirement for mentions) that this retirement decision was made against.

## AutoStory retirement does NOT retire the Telegram account fleet

No Telegram account, session, session encryption key, or authorization state was
touched by this retirement. Every account that could publish through AutoStory can
still publish through Manual Story, Discovery scanning still works against the same
fleet, and Readiness still checks the same accounts. See "Telegram fleet
preservation" below for exact before/after counts.

## What was removed

**Routes** (`src/dashboard/story_rotation_routes.py`) — all 13 `/api/stories/auto-campaigns/*`
endpoints (preview, fleet-summary, activity, list, active, create, policy-sweep, get,
dry-run, activate, run-now, pause, cancel). They now return 404 (no matching route)
rather than a half-functional handler.

**Scheduler tick** — `maybe_tick_story_rotation()` (the function that ticked due
AutoStory campaigns) was deleted entirely, not just flag-gated, along with its call
site in the shared scheduler loop (`src/scheduler/worker.py`). The scheduler's job
generation/claim/execute cycle — the general Scheduler product — is untouched.

**Admin UI** (`src/dashboard/templates/stories.html`) — the entire AutoStory
campaign-creation wizard: content/accounts/schedule/preview sections, the Active
Campaign card, the Recent Campaigns table, the campaign media library modal, the
campaign activity modal, and every JS function that only served them (~50 functions,
including `setWorkflowMode`, the whole `auto-*` form/preview/schedule/media/campaign
machinery). The page's Manual Story tools (Accounts/Media/Mentions/Run) are now the
only content — no longer behind a mode tab. Page title changed from "AutoStory" to
"Story".

**Tests** — `test_autostory_activity_humanization.py` and
`test_stories_per_day_honest_copy.py` (whole files; tested UI/JS that no longer
exists), plus one test function in `test_recurring_autostory_admin.py`
(`test_ui_primary_has_schedule_cta_no_pickup_slots_day`). The other 9 tests in that
file — which exercise the still-intact dormant backend logic (spad clamping,
`campaign_mode` resolution, wave caps, the preview formula) — were kept and
re-verified passing.

**Docs** — `docs/STAGED_UNLOCK_STORIES.md` (a staged-unlock procedure whose stages
all walked through creating/running AutoStory campaigns via UI paths and a scheduler
tick that no longer exist).

**Operational script** — `scripts/ops/autostory_canary_precheck.py` (confirmed
AutoStory-exclusive by import tracing).

## What was intentionally preserved (and why)

**Campaign creation/activation/run freeze (Phase 2, landed first)** —
`AUTOSTORY_CAMPAIGN_CREATION_ENABLED` (fail-closed, defaults false) gates
`create_campaign()`, `activate_campaign()`, and the operator-manual "Run now" path in
`execute_wave()`. This was the first, config-level freeze step, landed before the
route/UI removal in case anything needed to be paused quickly without a deploy.

**`auto_story_service.py` and `autostory_operator_preview.py`** — NOT removed.
Import tracing proved they don't become unused after the route and scheduler-tick
removal: `autostory_hardening.py` (kept — see below) imports `emit_autostory_system_log`
from `autostory_operator_preview.py`, which in turn imports `AWAKE_END_MINUTES`,
`AWAKE_START_MINUTES`, `compute_daily_times`, and `next_wave_after` from
`auto_story_service.py`. Both files are now dormant (no route or scheduler tick
calls into them) but still compile, still pass their existing tests, and stay
available as a rollback path if AutoStory is ever needed again.

**`autostory_hardening.py`** — NOT removed. `controlled_live_run.py`,
`mutation_boundary.py`, and `rotation_audit.py` (all part of the shared Manual Story
layer) import `account_in_active_wave_authorization` from it — one of several ways
the shared publisher can authorize a live send. With no AutoStory campaigns able to
exist anymore, this function simply always finds nothing to authorize; the other
authorization paths (env allowlist, legacy single-account) continue working normally.

**`autostory_media.py`** — NOT removed. `src/dashboard/routes.py` (the main shared
routes file) imports `prepare_story_derivative`/`validate_campaign_media` from it for
general media handling.

**`autostory_recurring.py`** — NOT removed. Imported by `autostory_media.py`
(kept, above) and the two files above it in this chain.

These four files were deliberately left in place rather than extracted or gutted.
The dead-code footprint is small; the risk of subtly breaking the shared Manual Story
authorization/media path through file-level surgery was judged not worth it. See the
in-conversation ownership-map discussion this retirement was based on for the full
import-tracing evidence.

**Database** — no tables dropped, no rows deleted. `AutoStoryCampaign`,
`AutoStoryAccountProgress`, `AutoStoryDailyProgress`, and `auto_story_account_locks`
remain in the schema as historical/audit data. `StoryRun`, `StoryRunStep`,
`StoryPool`, `StoryPoolMember` are shared with Manual Story and were never
AutoStory-exclusive. `Account`, `DiscoveredUser`, `SystemLog`,
`AccountReadinessSnapshot`, and every Scheduler/messaging table
(`ChatTarget`/`ScheduleProfile`/`ScheduleRule`/`ScheduledJob`/`MessageDelivery`) are
untouched.

**Media** — no AutoStory canary/test media files were deleted in this wave. Left for
a later, separate optional cleanup pass (Phase 19-equivalent) if ever wanted.

**Manual Story (shared "controlled live run" capability)** — fully preserved:
`controlled_live_run.py`, `client_lifecycle.py`, `publisher.py`,
`mutation_boundary.py`, `precheck.py`, `story_auth_state.py`, `rotation_audit.py`,
`story_readiness_resolver.py`, and the routes `/api/stories/{precheck,dry-run,runs,
readiness-preview,eligible-accounts,runtime-map}`. This is proven independently
usable — a `POST /api/stories/runs` call needs no `AutoStoryCampaign` at all — and is
how a real Story publish canary was run earlier this session with zero AutoStory
involvement.

**Telegram accounts, sessions, session encryption keys, authorization state,
Discovery data, Readiness infrastructure, the shared Telegram client manager, and
the general Scheduler product** — all untouched. See counts below.

## Environment variables

| Variable | Classification | Disposition |
|---|---|---|
| `AUTOSTORY_CAMPAIGN_CREATION_ENABLED` | AutoStory-only | New (this retirement); fail-closed default |
| `CAMPAIGN_EXECUTION_ENABLED` | Unrelated (governance/social-agent campaigns) | Untouched |
| `SCHEDULER_STORY_EXECUTION_ENABLED` | Was AutoStory-only | Now inert — nothing reads it; harmless to leave unset |
| `CONTROLLED_STORY_EXECUTION_ENABLED`, `STORY_MUTATIONS_ENABLED`, `STORY_ACCOUNT_MUTATION_ALLOWLIST`, `CONTROLLED_STORY_ACCOUNT_ID` | Shared (Manual Story gates) | Untouched |
| `AUTOSTORY_MENTIONS_PRODUCTION_CERTIFIED` | AutoStory-adjacent (mentions certification) | Untouched, stays `false` |
| Telegram API credentials, session encryption keys | Shared, load-bearing | Untouched |

## Rollback

Everything above is reversible through normal Git history — nothing was
force-pushed or rewritten (see Git section below). To restore AutoStory:
1. `git revert` (or cherry-pick out) the commits on `chore/retire-autostory`.
2. Set `AUTOSTORY_CAMPAIGN_CREATION_ENABLED=true` and `SCHEDULER_STORY_EXECUTION_ENABLED=true`.
3. Redeploy through the canonical `scripts/release/deploy_production.sh` tooling.

No database migration is needed either direction — nothing was dropped or altered.

## Git

Retired in a normal branch (`chore/retire-autostory`), not a history rewrite. Base:
`main` @ `3a78a5965e9892c49091d8a1af811382019c450a`. No commits were removed or
squashed away; the full AutoStory implementation remains recoverable from history
at any point before this branch.
