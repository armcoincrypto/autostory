# P6.5 First Canary Observation and Limited Manual Rollout Readiness

**Verdict:** `P6_5_FIRST_CANARY_OBSERVATION_AND_LIMITED_MANUAL_ROLLOUT_READINESS_PASS`

**Starting HEAD:** `849a43c`  
**Main goal:** Observe P6.4 canary, close delivery-recording gap, classify delivery 146, dual-rail safety, limited-rollout readiness — **no live Telegram send in P6.5**.

## Delivery persistence root cause

**Classification:** `legacy ownership mismatch` / missing call on gateway success path.

Certified gateway worker marked `TelegramGatewayJob` done and consumed P6.4 authorization after Telethon confirmed a message id, but never persisted `MessageDelivery` or marked `ScheduledJob` SENT (those lived on the scheduler executor rail / wrapper post-confirm). Wrapper inserted delivery **151** only after read-back.

**Canonical ownership (P6.5):** gateway rail owns idempotent `MessageDelivery` upsert after confirmed Telegram message id for certification markers.

## Ordering (implemented)

1. Telegram returns confirmed message id  
2. Idempotent `MessageDelivery` upsert (`gateway_job:{id}`, also matches `job_id+tg_message_id`)  
3. Mark GatewayJob done (result may include `SEND_CONFIRMED_DELIVERY_PERSISTENCE_PENDING` if step 2 failed)  
4. Mark ScheduledJob SENT (inside upsert)  
5. Consume authorization  
6. Operator stops gateway (manual)

**Critical rule:** persistence failure after confirmed send never resends Telegram.

## Dual-rail

`claim_due_job` excludes certification markers so the scheduler Telethon rail cannot claim gateway-owned certification jobs.

## Delivery 146

**Classification:** `TEST_RESIDUE_ARCHIVED`  
Mutated SENDING → FAILED with `error_code=TEST_RESIDUE_ARCHIVED` (row preserved). Evidence: `data/audit/p6_5_observation/delivery_146_classification.json`.

## Telegram 15975 (P6.5 re-read)

Absent now (deleted/removed after canary). Canary-time confirmation retained. Evidence: `data/audit/p6_5_observation/phase_c_telegram_readback.json`.

## Runbook / candidates

- Runbook: `docs/audits/P6_5_LIMITED_MANUAL_ROLLOUT_RUNBOOK.md`
- Candidates: `data/audit/p6_5_observation/phase_k_candidates.json`

## Next phase

`P6_6_LIMITED_OPERATOR_APPROVED_MANUAL_ROLLOUT` — at most 2–3 messages, one at a time, gateway rail only, after explicit operator approval.
