# P6.2 Readiness Worker Production Policy

## Ownership

| Concept | Canonical owner |
|---------|-----------------|
| Deep probe | `src/clients/readiness_worker._deep_check_one` |
| Worker loop | `src/clients/readiness_worker.readiness_worker_loop` |
| Standalone process | `scripts/run_readiness_worker.py` via `autostory-readiness-worker.service` |
| Persistence | `src/clients/readiness_store` → `account_readiness_snapshots` |
| Selection/backoff | `src/clients/readiness_worker_policy` |
| Manual refresh | POST `/operator/accounts/<id>/refresh-readiness` → `deep_check_one_for_operator` |
| Observability | `data/runtime/readiness_worker_status.json` + `/operator/system-safety` |

Web Gunicorn sets `READINESS_WORKER_ENABLED=false`. The systemd unit is the sole background owner.

## Eligible account selection

- Enabled (`status=active`) accounts only
- Not quarantined (`tier` not `reserved` or `controller`)
- Not AI-reserved IDs (110, 113, 131)
- Not `purpose` in (`autostory`, `ai_agent`)
- Has session material (file or string)
- Not active in runtime (`is_account_active`)
- Not in fresh READY trust window
- NOT_AUTHORIZED: backoff `READINESS_WORKER_AUTH_FAILURE_BACKOFF_SECONDS` (default 3600s)
- TEMP_CONNECT: backoff `READINESS_WORKER_TRANSIENT_BACKOFF_SECONDS` (default 300s)
- Optional allowlist: `READINESS_WORKER_ALLOW_IDS`

## Probe cadence

| Setting | Default |
|---------|---------|
| `READINESS_WORKER_CYCLE_SEC` | 60 |
| `READINESS_WORKER_ACCOUNT_DELAY_SEC` | 0.3 |
| `READINESS_WORKER_BATCH_SIZE` | 15 |
| `READINESS_WORKER_MAX_PARALLEL` | 1 |

## Concurrency

- One worker process (systemd)
- `READINESS_WORKER_MAX_PARALLEL` semaphore (default 1)
- Per-account probe slot prevents overlapping worker/manual probes
- Late results cannot overwrite newer state via per-account serialization

## Shutdown / restart

- SIGTERM sets stop event; loop exits after current cycle or interruptible sleep
- `TimeoutStopSec=30` on systemd unit
- Probe slots are in-memory only; restart clears locks

## Forbidden mutations

Worker must not create or mutate: `ScheduledJob`, `TelegramGatewayJob`, `MessageDelivery`, authorization manifests, send counters, schedule state.

## Telegram probe classification

Read-only: connect, `get_me`, authorization check, membership/permission inspection where canonical.

Must not: send messages, join/leave targets, consume gateway authorization.

## NO_GO preservation

Worker does not change `AUTOSTORY_PRODUCTION_CERTIFIED_NO_GO`, generation flags, or scheduler mutation flags.
