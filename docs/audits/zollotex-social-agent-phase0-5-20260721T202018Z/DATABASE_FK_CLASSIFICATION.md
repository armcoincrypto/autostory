# Database Foreign-Key Classification

## Evidence boundary

- Live database: `/opt/autostory/data/storyfleet.db` (read-only inspection only)
- Consistent backup method: Python `sqlite3.Connection.backup`
- Restricted baseline copy: outside Git, mode `0600`
- Baseline/copy SHA-256 before rehearsal: `44bcc92934427a8c40ec51ebda81e0f5963eaae8b5dd882378c8c089898cd0a2`
- `PRAGMA quick_check`: `ok`
- `PRAGMA integrity_check`: `ok`
- Exact `PRAGMA foreign_key_check` count: **910**
- Exact classified count: **910**

The source database reports `PRAGMA foreign_keys=0` on a new raw SQLite connection. Application-created connections attempt to enable it, so enforcement depends on entry point. This explains how orphan rows can persist but is not itself a repair.

## Reconciled families

| Child table | Parent | FK column | Count | Classification | Business significance |
|---|---|---|---:|---|---|
| `account_risk_events` | `accounts` | `account_id` | 737 | `ORPHANED_CHILD_AFTER_PARENT_DELETE` | Historical precheck events from March-April 2026; parent account no longer exists |
| `schedule_profiles` | `accounts` | `account_id` | 78 | `ORPHANED_CHILD_AFTER_PARENT_DELETE` | Unreachable configuration; cannot execute without an account |
| `schedule_rules` | `accounts` | `account_id` | 78 | `ORPHANED_CHILD_AFTER_PARENT_DELETE` | Unreachable PROMO rules; cannot execute without an account |
| `message_deliveries` | `chat_targets` | `target_id` | 6 | `ORPHANED_CHILD_AFTER_PARENT_DELETE` | Historical `FAILED` deliveries |
| `message_deliveries` | `accounts` | `account_id` | 3 | `ORPHANED_CHILD_AFTER_PARENT_DELETE` | Historical `FAILED` deliveries |
| `account_target_membership_probes` | `accounts` | `account_id` | 4 | `ORPHANED_CHILD_AFTER_PARENT_DELETE` | Historical `error` probes |
| `scheduled_jobs` | `chat_targets` | `target_id` | 4 | `ORPHANED_CHILD_AFTER_PARENT_DELETE` | Historical `FAILED` jobs |
| **Total** |  |  | **910** |  |  |

All 910 violations correspond to 910 distinct child rows. No row violates more than one constraint.

## Parent evidence

The classifier found 79 distinct missing account identifiers and one missing target identifier. A metadata-only scan of 205 historical SQLite backups found none of those parent rows. There is therefore no authoritative parent record available for restoration. Creating synthetic account or target rows would be dishonest and could reactivate stale schedules.

## Safety decision

The rehearsal archives each complete orphan row inside the copied database, then removes only the orphan child. It does not fabricate parents, reattach children speculatively, disable constraints, or touch live rows.
