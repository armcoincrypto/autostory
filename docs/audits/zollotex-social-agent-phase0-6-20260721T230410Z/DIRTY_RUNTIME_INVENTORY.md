# Dirty Runtime Inventory

Latest `/opt/autostory` observation: branch `claude/deploy-bot-vps-WbBG6`, HEAD `499758e`,
1,428 dirty paths (35 modified, 1,393 untracked).

| Class | Evidence / count | Decision |
|---|---|---|
| Canonical product code | Active import closure under `src`; source-backed AI, scheduler, readiness, governance, dashboard modules | Canonicalized in reviewed commits and tested; no automatic deployment |
| Canonical tests | Focused dependency, AI, security, and Story safety tests | Canonicalized where tied to accepted source |
| Generated runtime artifact | 1,019 `data/*` entries, audit output, reports, media and transient state | Excluded from Git candidate |
| Secret/local configuration | active `.env`, 14 root backup names, local Cursor state | Excluded; permissions retained at `0600`; rotations required |
| Log/cache | logs, bytecode, caches, lock/transient output | Excluded |
| Database/session state | SQLite files, Telegram sessions, `tmp_session_fix` | Excluded; no migration or row change |
| Obsolete/unsafe | `src/stories/run_batch.py` placeholder; `src/dashboard/routes.py` bytecode wrapper; source backups | Rejected |
| Unknown/owner required | 29 recovery modules and associated phase scripts/tests | Not needed by active accepted import closure; owner must review before canonicalization |
| Bytecode-dependent stopped service | Kathleen package wrappers plus ignored `_bytecode_archive` | Rejected; source recovery required before service can be reproducible |

No tracked deletion was observed in the production status. Modification time was not used as
sole provenance. Exact runtime files imported into the candidate were hashed into Git blobs,
reviewed for callers, scanned for high-confidence secret patterns, and exercised by collection.
