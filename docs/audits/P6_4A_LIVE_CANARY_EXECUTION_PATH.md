# P6.4A — Live canary execution-path hardening (architecture & TOCTOU)

## Verdict target

`P6_4A_LIVE_CANARY_EXECUTION_PATH_HARDENING_AND_DRY_RUN_PASS`

## Canonical gateway path

```text
ScheduledJob (marker __p6_4_certification__)
  → telegram_gateway_jobs enqueue (payload: text, hash, target_id, binding_id, job_marker)
  → claim_next_jobs (DB atomic claim)
  → AI allowlist + P6.4 gateway claim extra account ids
  → content hash recompute vs expected_message_sha256
  → can_execute_action (execution_guard)
       → P6.4 authorization (armed manifest: job/account/target/binding/hash/TTL/max_sends=1)
       → send-time binding guard → classify_binding_verification (shared P6.3)
  → optional P5D certification reconciliation (ambiguous → no auto-retry)
  → TelegramDirectTransport.send_message_async
  → mark_job_done + mark_consumed (P6.4)
  → wrapper stops/disables telegram-gateway
```

No parallel send route was added.

## Send-time binding guard

Source: `src/clients/send_time_binding_guard.py`

Reuses `classify_binding_verification` (no duplicated status engine).

Rejects missing/wrong binding link and any non-`VERIFIED_CAN_POST` / non-production-verified
outcome (stale, configured-only, not joined, no permission, quarantined, not ready, etc.).

## Binding freshness TTL

`BINDING_VERIFICATION_FRESH_TTL_SEC` default **3600** (1 hour), from
`src/clients/binding_verification.py`.

For the one-job canary this is sufficient because:

1. Operator runs fresh execution-preflight immediately before send.
2. Auth TTL is **15 minutes** (stricter than binding TTL).
3. Gateway window is seconds-to-minutes with immediate shutdown after terminal result.
4. No live Telegram probe at send time (avoids pre-send uncertainty racing the readiness worker).

## TOCTOU policy

```text
eligibility / binding refresh at preparation
  → persist binding_id + content hash on job/authorization
  → send-time guard re-reads persisted verification + TTL
  → account readiness freshness still enforced inside classify_binding_verification
  → NO additional live Telegram probe immediately before send
```

## Authorization

`src/core/p6_4_authorization.py` — P5D-style file manifest, scope hard-coded:

| Field | Value |
|-------|-------|
| account | 107 |
| target | 1 |
| binding | 39 |
| max_live_sends | 1 |
| auth TTL | 15 minutes |
| content | SHA-256 of exact UTF-8 body |

Rejects wrong job/account/target/binding/content, expiry, consume, invalidate, unarmed flag-off.

## Content canonicalization

- Hash = SHA-256 of UTF-8 bytes of the exact approved string.
- No NFC normalization, no trailing-whitespace strip, no CRLF conversion.
- Line endings and trailing spaces are significant.
- Link preview / entities / formatting mode are not rewritten by the gateway;
  the payload `text` field is what Telegram receives.
- Declared `expected_message_sha256` must match recomputed hash or the job fails
  with `content_hash_mismatch` before send.

## Duplicate / uncertain send

- Atomic gateway claim + terminal job status.
- Consumed authorization blocks replay.
- Certification payloads use existing P5D reconciliation; ambiguous outcomes set
  `ambiguous_reconciliation_required` without automatic retry.
- Prefer one send + manual reconciliation over speculative retries.

## Operator scripts

| Script | Role |
|--------|------|
| `scripts/ops/p6_4_live_canary_preflight.py` | `--dry-run` / `--execution-preflight` (read-only) |
| `scripts/ops/p6_4_execute_live_canary.py` | One-job wrapper (do not run until P6.2 PASS + approval) |
| `scripts/ops/p6_4_emergency_stop.sh` | Idempotent gateway stop + flag restore + auth invalidate |

## Safety invariants during P6.4A

```text
AUTOSTORY_PRODUCTION_CERTIFIED_NO_GO=true
PROMO_GENERATION_MODE=disabled
SCHEDULER_MUTATIONS_ENABLED=false
P5C/P5D/P6_4_SINGLE_SEND_ENABLED=false
telegram-gateway inactive+disabled
```

P6.2 soak worker and checkpoint timer must remain untouched.
