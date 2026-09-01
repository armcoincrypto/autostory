# AutoStory Product Policy — Verified Truth (2026-09-01)

Date: 2026-09-01
Wave: combined convergence (`release/autostory-final-product-convergence`) +
blocked_until scheduler hardening + honest Stories/day copy
Status: Accepted. Deployed to production at `MAIN_SHA=0c9413c4fa7da37ccda0b2c45e76dfb19d1b1858`.

This document is the durable record of what AutoStory is actually certified
to do, based on real production evidence — not what the source code
architecturally supports. Source support and production certification are
deliberately different things in this product; this file is the single
place that states the certified truth plainly.

## Stories per account per day

| Setting | Status | Evidence |
|---|---|---|
| 1/day | **Production certified** | Repeated real production publishes, no capacity rejection. |
| 2/day | **Capability dependent — not universally certified** | Two real wide-spacing canaries, two different Telegram-side rejections (below). |
| 3/day | **Not production certified** | Never attempted in production; blocked by policy until 2/day is resolved further. |

### The two 2/day production canaries

**Campaign #18** (2026-08-29, account 121): Story 1 published successfully.
Story 2 was rejected by Telegram:
```
RPCError 400: STORIES_TOO_MUCH (caused by CanSendStoryRequest)
```
This incident also exposed and led to fixing a real scheduler bug: a 45-second
tight retry loop that hammered Telegram ~100+ times over ~90 minutes before
`ends_at` forced the campaign to stop. See
[AUTOSTORY_STORY_CAPACITY_RETRY_INCIDENT](AUTOSTORY_STORY_CAPACITY_RETRY_INCIDENT.md)
(referenced in `tests/test_autostory_story_capacity_retry_hardening.py`) and
the fix landed same-day (retry-after regex fix, `story_blocked_until`
consultation, backoff on `fresh_story_auth_failed`).

**Campaign #20** (2026-09-01, account 121, re-run with wide 12h spacing after
the Wave 1B hardening): Story 1 *itself* was rejected before ever publishing:
```
RPCError 420: STORY_SEND_FLOOD_WEEKLY_1788445835 (caused by CanSendStoryRequest)
blocked_until: 2026-09-02T04:00:22Z (24h block)
```
This is a different, stricter Telegram-side limit than Campaign #18's — a
weekly flood cap, not a same-day capacity cap. It confirms 2/day capacity is
genuinely gated by Telegram's own per-account state, not by anything
AutoStory's scheduler controls, and that no amount of "better spacing" can
guarantee around it.

**Scheduler behavior on Campaign #20 was correct in the ways that matter**:
exactly one real `CanSendStoryRequest` call was made; every one of the ~30
subsequent scheduler ticks over the following ~8 hours used the durable
`story_blocked_until` short-circuit instead of calling Telegram again; no
duplicate `SystemLog` spam; no leaked locks or claims; the shortfall was
recorded honestly (`successful_count=0, target_count=2`), never hidden. The
one real inefficiency found — the scheduler still re-claimed the campaign
every 15 minutes for those 8 hours instead of waiting for something closer to
the actual 24h `blocked_until` — was fixed the same day (see "Scheduler
hardening" below).

### Product policy going forward

Per this evidence, the recommended and now-implemented policy is:
```
1/day = universally production-certified
2/day = Telegram-capability dependent (per-account, per-week; not
        something AutoStory can force or guarantee)
3/day = not production-certified
```
The `/stories` admin UI (`src/dashboard/templates/stories.html`,
`id="auto-spad"`) reflects this directly: "1 — Production certified",
"2 — Capability dependent", "3 — Not certified", with copy stating
Telegram may restrict some accounts and AutoStory will never force
publication beyond Telegram's capacity. Default selection is 1. Source
support for 2 and 3 is unchanged — this is a certification/labeling
policy, not a capability removal.

The next 2/day step, if pursued, is a small multi-account (e.g. 3 accounts
× 2/day) canary to see whether the flood/capacity limits are truly
per-account-independent — not another single-account retry with different
spacing.

## Scheduler hardening: blocked_until-aware next_wave_at

Campaign #20 exposed one remaining inefficiency: when every account in a
wave is blocked, `next_wave_at` was a flat
`FRESH_AUTH_FAILURE_BACKOFF_MINUTES` (15 min) retry regardless of how far
out the real block actually was. Fixed in `execute_wave()`
(`src/stories/auto_story_service.py`): `next_wave_at` now tracks the
earliest `story_blocked_until` among the failed accounts (falling back to
the flat backoff only when an item has no known block duration). If that
earliest time is at or past the campaign's `ends_at`, `next_wave_at` is
left unset rather than scheduling a retry that could never fire — the
pre-existing `ends_at` expiry sweep in `tick_due_auto_story_campaigns()`
already completes such a campaign honestly. No new scheduler; reuses
`story_blocked_until`, `next_wave_at`, and the existing deferred-state
machinery. See `tests/test_autostory_story_capacity_retry_hardening.py`
(`test_single_account_blocked_24h_100_ticks_near_zero_calls`,
`test_25_accounts_1_blocked_24_continue`,
`test_all_accounts_blocked_next_wave_at_uses_earliest_blocked_until`,
`test_blocked_until_exceeds_campaign_ends_at_no_pointless_polling`).

## Discovery mentions: implemented, durable, not production-certified

Durable random mention selection is fully implemented and tested locally
(23 tests, `tests/test_autostory_durable_mentions.py`): per-(campaign,
wave, account) slot storage on `AutoStoryAccountProgress.mention_plan`
(write-once, retry-reuse, all-or-nothing fail-closed on partial state),
self-mention exclusion, no duplicate targets within a wave, safe handling
of pools smaller than requested, and safe skip-not-crash behavior for
unresolvable targets. `AUTOSTORY_MENTIONS_PRODUCTION_CERTIFIED` remains
`false` in production (`/opt/autostory/.env`) and must not be flipped
until a real live mention canary passes.

### First live mention canary attempt (2026-09-01): blocked by a Telegram platform prerequisite, not a defect

Ran the first real controlled-live-run mention canary on account 107 (the
only account passing every exclusion filter — not AI-reserved, not
protected) with one mention (`@Gor_Verdyan`, sourced from Binance Armenia,
`source_chat_id=1936532075`) after confirming a fresh `CanSendStory:
allowed` precheck. Every app-side gate passed correctly. The real
`SendStoryRequest` was then rejected by Telegram itself, before accepting
anything:
```
PremiumAccountRequiredError: A premium account is required to execute
this action (caused by SendStoryRequest)
```
This is consistent with Telegram's documented behavior: posting a Story
with a caption or entity (including a mention) requires the publishing
account to have Telegram Premium, even though plain Stories (no caption,
no mentions — exactly what Campaign #18/#20 used) do not. Confirmed clean
and safe: rejected pre-send (`cleanup.ok: true`, no `story_id`, no
ambiguity), zero Story/message counter deltas.

**A read-only fleet inspection found Telegram Premium status is not
stored or discoverable anywhere in existing local data** — no DB column,
no account notes/risk_notes annotations, and 30 days of production logs
show zero prior capture of the field despite `get_me()` /
`GetFullUserRequest` being called routinely during health checks (the
verbose health-check step extracts a curated `user_flags` dict --
`deleted`, `restricted`, `bot`, `scam`, `fake` -- that never included
`premium`, even though Telethon's `User` object carries it). Determining
Premium status for any account requires a new, dedicated live Telegram
query per account — out of scope until a documented Premium account is
identified.

**The live mention canary (Waves 7–9 of the certification plan) is on
hold until a Storyfleet account with confirmed Telegram Premium is
identified.** `AUTOSTORY_MENTIONS_PRODUCTION_CERTIFIED` stays `false`
until that canary passes for real.

## Deployment

`MAIN_SHA=0c9413c4fa7da37ccda0b2c45e76dfb19d1b1858`, deployed via the
canonical `scripts/release/deploy_production.sh` (dry-run PASS, then real
deploy PASS): all 3 services (`autostory-web`, `autostory-scheduler`,
`autostory-readiness-worker`) confirmed independently on the same release
directory, `current` symlink correct, `STORIES_DELTA=0`,
`MESSAGES_DELTA=0`. Rollback target
(`/opt/autostory-releases/20260830T133745Z-800270c19292`, the pre-hardening
baseline) confirmed intact with a valid manifest; `rollback_production.sh`
read through in full and syntax-checked (no live rollback performed —
nothing is broken). Full deploy audit trail, including a 62MB DB backup
with a passed integrity check, preserved under
`/var/lib/server-ops/audits/autostory-deploy-20260901T124640Z/` on
production.

Full test suite at this SHA: **1061 passed, 0 failed, 13 skipped**, both
Python 3.11 and 3.12.
