# P6.4 Single-Account Single-Target Live Canary — Final Evidence

**Verdict (prior phase):** `P6_4_SINGLE_ACCOUNT_SINGLE_TARGET_OPERATOR_APPROVED_LIVE_CANARY_PASS`  
**Certified commit:** `849a43c`  
**Evidence root:** `data/audit/p6_4_live_canary/`  
**Canonical run directory:** `data/audit/p6_4_live_canary/20260714T161351Z/`

## Pair

| Field | Value |
|-------|-------|
| Account | 107 |
| Target | 1 (`cryptodiscussing` / tg `1775722510`) |
| Binding | 39 |
| Authorization | `d94775da-ae4d-4e20-8ad4-533680a6249a` (consumed) |
| ScheduledJob | 369 SENT |
| GatewayJob | 6071 done |
| Delivery | 151 SENT |
| Telegram message ID | 15975 (confirmed at canary time) |
| Content SHA-256 | `a5d478e8e99395333e300ff0479fea14a89cea0f6873376cb0f9839ea5462e55` |

## Artifact index

| Artifact | Path |
|----------|------|
| Approved content | `data/audit/p6_4_live_canary/approved.txt` |
| Approved-content manifest | `data/audit/p6_4_live_canary/approved_content_manifest.json` |
| P6.2 soak certification | `data/audit/p6_2_soak_cert_20260714T161621Z.json` (and baseline `p6_2_soak_baseline_20260713T121009Z.json`) |
| Execution preflight | see execute day checklist + attempt before-state `20260714T161351Z/step6_before_state.json` |
| Attempt 1 result | `20260714T161351Z/execute_result.json` |
| Attempt 2 result | `20260714T161351Z/execute_result_attempt2.json` |
| Telegram read-back (canary time) | `20260714T161351Z/step9_confirmation.json` |
| Transport-scope fix | commit `849a43c` |

## Why attempt 1 was safe

1. Execution guard initially ALLOW under armed P6.4 authorization.
2. `TelegramDirectTransport.send_message_async` re-guard DENY with `scheduler_mutations_disabled` because certification scope (`job_marker` / `target_id` / `binding_id` / `content_sha256`) was stripped before the transport layer.
3. **No Telegram request** was issued for the live send path.
4. Authorization was invalidated for the failed attempt path.
5. Scheduled job was cancelled; gateway job **6070** terminally failed.
6. **No MessageDelivery** and **no Telegram message** for attempt 1.

## Why attempt 2 is canonical

1. Fix `849a43c` propagates certification scope into transport `require_execution_allowed`.
2. Exactly **one** authorization (`d94775da-…`) reserved/consumed.
3. Exactly **one** ScheduledJob **369**, **one** GatewayJob **6071**, **one** Telegram request, **one** confirmed message ID **15975**.
4. Content hash matched approved bytes; authorization consumed once; no retry; no duplicate delivery for tg mid 15975.
5. Gateway stopped/disabled after send; fail-closed flags restored.

## Delivery-finalization gap (observed)

Gateway marked job done + consumed auth, but did **not** auto-create `MessageDelivery`. Delivery **151** was inserted after Telegram read-back proof in the wrapper confirmation step. P6.5 closes this gap on the gateway success path with idempotent upsert (`gateway_job:{id}`).

## P6.5 follow-up observation (2026-07-14)

Read-only re-fetch of message **15975** found the message **absent** from the channel (neighbor ID **15974** exists; **15975** is a gap; approved text absent in last 500 messages). Classification: **deleted/removed after canary**, not a failed send. Canary-time proof remains journal `ai_agent_send_ok … telegram_message_id=15975` + `step9_confirmation.json`.
