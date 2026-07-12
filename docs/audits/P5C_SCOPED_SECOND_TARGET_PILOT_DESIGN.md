# P5C — Scoped Second-Target Single-Send Pilot (Design Only)

**Status:** `DESIGN_ONLY_NOT_ARMED` — no job, no manifest armed, no send  
**Prepared by:** P5B.2 architecture phase  
**Overall production status:** `AUTOSTORY_PRODUCTION_CERTIFIED_NO_GO`

## Recommended candidate (technical eligibility)

| Field | Value |
|-------|-------|
| target_id | **14** |
| display_name | Saved Messages (P9.45 operator self) |
| telegram_peer_id | **8000295303** |
| target_type | **private** |
| binding_id | **45** |
| account_id | **107** |
| risk_tier | **TIER_0_SELF_CONTROLLED** |

### Why this target

- Genuinely different from `cryptodiscussing` (group → private Saved Messages path)
- Operator-controlled (self-chat for account 107)
- Binding exists with `can_post=true`
- Lowest risk tier in selection order
- Does not require external community permission

### Pre-arm blockers (P5C prep)

1. **Operator approval** — `PENDING_EXPLICIT_OPERATOR_CONFIRMATION`
2. **Membership probe** — no recent probe row; P5C prep must run non-mutating probe
3. **Account readiness** — refresh before arm if expired

## Rejected candidates (summary)

| target_id | username | Primary blockers |
|-----------|----------|------------------|
| 4 | infografikawbchat | no_binding (probe only) |
| 6, 7, 10 | various | not_joined, no_binding |
| 9 | — | peer unresolved, no_binding |
| 15+ | Saved Messages (other accounts) | no_binding for account 107 |

External groups without binding are **TIER_4** — prohibited for automatic selection.

## Independent authorization design

P5C must **not** reuse P4C/P5A authorization, counters, jobs, or deliveries.

| Artifact | Path |
|----------|------|
| Manifest | `data/audit/p5c_scope_manifest.json` |
| Counter | `data/audit/p5c_single_send_state.json` |
| Runner (future) | `scripts/ops/p5c_scoped_second_target_pilot.py` (to be implemented at P5C) |
| Env flag | `P5C_SINGLE_SEND_ENABLED=false` until explicit arm |

## Proposed message (template)

```
P5C controlled second-target connectivity test <UTC_TIMESTAMP>. No action required.
```

Hash computed at P5C `--prepare` from timestamped body.

## Execution constraints

- max_live_sends = 1
- max_jobs = 1
- max_deliveries = 1
- allow_retry = false
- scheduler + gateway stop before execute
- uncertain send → no retry, incident reconciliation

## PASS / FAIL criteria (future P5C)

**PASS:** one fresh job, one delivery, one tg_message_id, target 14 peer 8000295303, P5C counter 1/1, P4C/P5A evidence unchanged, no side effects.

**FAIL:** duplicate send, wrong target, counter reuse, global mutations enabled, account 139 used.

## Operator action required

Before P5C may be armed, operator must explicitly confirm:

```text
target_id = 14
telegram_peer_id = 8000295303
account_id = 107
```

Document approval in manifest `operator_approved: true`.

## Machine-readable design

`data/audit/p5c_second_target_candidate.json`
