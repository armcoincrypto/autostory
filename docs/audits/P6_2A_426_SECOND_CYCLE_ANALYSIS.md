# P6.2A — 426-Second Cycle Root Cause Analysis

## Observed

- **cycle_id:** `3312200d-1b7c-4099-b97a-5b26cae3d48b`
- **cycle_started:** `2026-07-13T11:09:54Z` (13:09:54 CEST)
- **first probe:** `2026-07-13T11:16:46Z` (account 118)
- **cycle_completed:** `2026-07-13T11:17:00Z`
- **cycle_duration_sec:** `426.27`
- **checked:** `10` (all `ready`)

## Classification

**Confirmed fact:** Wall-clock cycle duration matches logged `cycle_duration_sec` (not a metrics defect).

**Confirmed fact:** Actual Telegram probes for this cycle took ~14 seconds total (10 accounts × ~0.8s each).

**Confirmed fact:** ~412 seconds elapsed between `cycle_started` and the first `account_probe_started` with no intervening worker logs.

**Inference:** The worker event loop was blocked on synchronous work between cycle start and probe dispatch — most likely **SQLite database lock contention** from a concurrent full `pytest tests/` run (PID 8910 started ~11:03 UTC, overlapping the cycle).

**Inference:** Pre-P6.2A candidate selection ran synchronously on the asyncio event loop inside `get_db_context()`, amplifying stall visibility in cycle duration.

## Remediation (P6.2A)

1. Move `select_probe_candidates()` to `run_in_executor()` — **implemented**
2. Split metrics: `selection_duration_sec`, `probe_duration_sec`, `cycle_duration_sec` — **implemented**
3. Emit `slow_cycle_detected` when duration exceeds `READINESS_WORKER_SLOW_CYCLE_WARN_SEC` (default 120s) — **implemented**
4. Checkpoint script flags slow cycles as warnings — **implemented**

## Outcome

**Harmless under normal operation** when no concurrent DB writers contend. Under contention, cycles may appear slow without unsafe probe overlap. Not a certification blocker after observability split and executor offload.
