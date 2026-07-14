# P6.5 Limited Manual Rollout Runbook

**Scope:** operator-approved manual sends only (future P6.6).  
**This document does not authorize any send.**  
**Rail:** certified telegram-gateway only. Scheduler direct Telethon executor must not claim certification jobs.

## Absolute rules

1. No second send until the previous send is fully confirmed and observed.
2. One message at a time; max **2–3** additional messages total for the initial rollout window.
3. One approved account–target–binding tuple at a time.
4. Manual content approval + exact SHA-256; fresh preflight; `max_sends=1`.
5. Gateway inactive/disabled before each send; stopped after each send.
6. Do not enable scheduler mutations, fleet execution, campaigns, Discovery, Broadcast, or Stories publishing.
7. Do not use account or target fallback. Do not batch.

## Candidate selection

1. Load `data/audit/p6_5_observation/phase_k_candidates.json`.
2. Prefer `public_target_validation` for business proof; use `self_target_validation` only to prove rail/delivery (not the channel goal).
3. Exclude: NOT_AUTHORIZED / STALE READY / quarantined / `can_post=false` / unresolved membership / unresolved target identity / sticky reconciliation / active job / pending gateway job.
4. Require **explicit operator approval** of the exact `(account_id, target_id, binding_id)` before arming.

## Content approval

1. Operator writes final text to a frozen file (UTF-8, no surprise trailing newline unless intentional).
2. Compute SHA-256 of exact bytes; record in authorization manifest.
3. Content must not reuse a previous authorization’s exact hash for a new live send without a new authorization record.

## Prerequisite verification

- [ ] P6.2 readiness soak still green (readiness worker active).
- [ ] P6.4 canary history understood (`docs/audits/P6_4_SINGLE_ACCOUNT_SINGLE_TARGET_LIVE_CANARY_FINAL.md`).
- [ ] P6.5 delivery persistence fix deployed (gateway upserts `MessageDelivery` on confirmed mid).
- [ ] Flags fail-closed: `AUTOSTORY_PRODUCTION_CERTIFIED_NO_GO=true`, `PROMO_GENERATION_MODE=disabled`, `SCHEDULER_MUTATIONS_ENABLED=false`, `P5C/P5D/P6_4_SINGLE_SEND_ENABLED=false`, `DISCOVERY_EXECUTION_ENABLED=false`.
- [ ] `telegram-gateway` inactive + disabled before arming.

## Execution preflight

Reuse `scripts/ops/p6_4_live_canary_preflight.py` patterns (or successor P6.6 script) for the approved tuple:

- account READY, readiness fresh
- account authorized, not quarantined
- target enabled; binding `VERIFIED_CAN_POST` / fresh membership
- no unresolved reconciliation; no active job; no pending gateway job
- no prior live authorization for this exact content hash

## Before-state capture

Record counts and IDs: pending jobs, pending gateway jobs, SENDING deliveries, readiness NRestarts, HEAD SHA, flag values, gateway ActiveState.

## Single execution

1. Arm authorization (`max_sends=1`, exact hash, tuple, TTL).
2. Create certification ScheduledJob + gateway job via approved wrapper only.
3. Start gateway **once** for the single job.
4. Observe terminal states; stop + disable gateway immediately after terminal result.

## Live observation

Confirm:

- gateway job `done` with `telegram_message_id`
- ScheduledJob `SENT`
- exactly one `MessageDelivery` SENT with that tg mid
- authorization consumed once
- no second job/auth/gateway/delivery

## Telegram read-back

Read-only fetch by message id. Confirm text/hash/target. Do not edit or delete.

## Delivery verification

Delivery must exist **automatically** from gateway finalization (P6.5). If persistence pending (`SEND_CONFIRMED_DELIVERY_PERSISTENCE_PENDING`): **do not resend**; retry persistence / reconcile only.

## Authorization verification

Manifest: `consumed=true`, `armed=false`, single authorization id.

## Gateway shutdown verification

`systemctl is-active telegram-gateway` → inactive; `is-enabled` → disabled.

## Uncertain-outcome handling

| Outcome | Action |
|---------|--------|
| Pre-send denial | No Telegram send; invalidate auth; no delivery |
| Confirmed mid + delivery persist fail | Reconcile persistence only — never resend |
| Timeout / no mid | Treat as uncertain; Telegram history lookup; no blind retry |
| Duplicate suspicion | Stop; compare hashes and message ids |

## Emergency stop

`scripts/ops/p6_4_emergency_stop.sh` (or equivalent): disarm flags, stop gateway, invalidate unconsumed auth.

## Post-send observation

Observe ≥ one interval: no delayed retry in scheduler/gateway logs; pending counts zero; NRestarts stable; Operator UI shows SENT/done/SENT with tg mid.

## Dual-rail note

Scheduler claim excludes `__p5c_certification__` / `__p5d_certification__` / `__p6_4_certification__` jobs. Prefer code exclusion; do not rely solely on manually stopping the scheduler.
