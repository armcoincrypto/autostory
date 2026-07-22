# Ending Identity (recaptured after concurrent operator change)

Capture UTC: approximately `20260722T115400Z`

| Field | Value |
|---|---|
| Web WD / SHA | `/opt/autostory-releases/20260722T114439Z-9b3ccc0a102e` / `9b3ccc0a102e687d3bae4116745f424a78f878f4` |
| Scheduler WD / SHA | `/opt/autostory-releases/20260722T090829Z-c9d1fe614bb0` / `c9d1fe614bb0b764805f532d9766bd35bbbb7ed0` |
| Readiness WD / SHA | `/opt/autostory-releases/20260722T114439Z-9b3ccc0a102e` / `9b3ccc0a102e687d3bae4116745f424a78f878f4` |
| SESSION_ENCRYPTION_MODE | `disabled` (all three) |
| AI_AGENT_AUTO_LOOP_ENABLED | `false` |
| SCHEDULER_MUTATIONS_ENABLED | `false` |
| Production session enc rows | 0 |
| Production session plain rows | 104 |
| Phase 0.8 branch tip | `b1f46f435c1ac52cb1d2d092a661b809f2798b71` |
| Phase 0.8 production deploy | **no** |

## Drift note
During Phase 0.8 rehearsal, another operator promoted web+readiness to branch `fix/autostory-canary-blockers-clearance-20260722` release `9b3ccc0`, leaving scheduler on Phase 0.7 `c9d1fe6`. This **splits** the Phase 0.7 single-SHA invariant. Encryption mode and session rows were not migrated by Phase 0.8.
