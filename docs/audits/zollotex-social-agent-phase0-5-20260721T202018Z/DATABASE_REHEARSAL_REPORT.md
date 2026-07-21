# Database Repair Rehearsal

## Result

```text
FOREIGN_KEY_VIOLATIONS_BEFORE=910
FOREIGN_KEY_VIOLATIONS_AFTER=0
```

- Dry-run rows selected: 910
- Archived rows: 910
- Deleted orphan rows: 910
- Second execution archived/deleted: 0
- `PRAGMA quick_check`: `ok`
- `PRAGMA integrity_check`: `ok`
- Reviewed business aggregates stable: yes
- Live database rows modified: no

## Row-count deltas

| Table | Delta |
|---|---:|
| `account_risk_events` | -737 |
| `account_target_membership_probes` | -4 |
| `message_deliveries` | -9 |
| `schedule_profiles` | -78 |
| `schedule_rules` | -78 |
| `scheduled_jobs` | -4 |
| `phase05_fk_orphan_archive` | +910 |

Active-account count, sent-delivery count, pending/running job counts, and published-story count were unchanged.

## Application verification

- Focused classifier/repair tests: passed.
- Actual dirty runtime source successfully created the Flask app against the repaired copy, returned HTTP 200 from liveness, and read 104 accounts/366 scheduler rows with all background workers explicitly disabled.
- The committed `017d326` source cannot create the full Flask app because `src/core/database.py` and `src/dashboard/app.py` import untracked production-only `ai_agent_models.py` and `ai_agent_routes.py`. This is a release-lineage blocker, not a database-repair failure.

An initial smoke attempt omitted `READINESS_WORKER_ENABLED=false`; the dirty runtime source started one readiness cycle against the copied database before the short-lived process aborted at interpreter shutdown. No production database was selected, no content/message mutation path was called, and the corrected smoke was rerun with readiness and AI loops disabled. This startup side effect must be eliminated from future test harnesses.
