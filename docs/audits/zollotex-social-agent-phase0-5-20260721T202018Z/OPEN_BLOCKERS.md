# Open Blockers

| Blocker | Owner | Required action |
|---|---|---|
| Production runs from a dirty source worktree while the Phase base cannot collect the full test suite or boot the complete app from tracked files | AutoStory repository/release owner | Reconcile every runtime-only source file into reviewed Git lineage, remove bytecode-only route dependency, produce a clean immutable release, and pass the full suite |
| Critical public Swaperex monitoring ingest, wallet-scan amplification, diagnostic RPC/explorer routes, permissive CORS, proxy trust/rate-limit defects, and direct `0.0.0.0:4001` bind remain | Swaperex backend/operations owner | Implement and certify compatibility-safe hardening in the Swaperex repository; verify firewall and consumers |
| Swaperex admin token was exposed during evidence capture | Swaperex security/operator owner | Remove build-time client token fallback, map consumers, rotate token urgently, and verify old-token rejection |
| AutoStory bot/API/admin credentials remain exposed in Git history and backups | Security/provider/repository owners | Execute reviewed rotation matrix, remove files from canonical branches, decide coordinated history rewrite, scan fresh clone/releases/caches |
| Telegram DB encryption is not deployed; SQLite session files, bot sessions, backups, browser continuation, bot paste import, raw SQL, direct resolver bypasses, and two incompatible Fernet helpers remain | AutoStory/Telegram security owner | Consolidate resolver and producers, eliminate fail-open Redis behavior, design file-session vault/materialization, replace opaque bytecode routes, provision durable key, then approve staged production migration |
| FK repair is rehearsed only; production still has 910 violations | Data owner/release manager | Approve archival semantics and maintenance/concurrency runbook only after immutable release/full tests pass |
| AI auto loop is enabled while configured Telegram gateway service is inactive; Kathleen config says enabled while service is inactive | AutoStory runtime owner | Decide intended ownership/state, reconcile flags and services under a separate approved change, and update canonical units |
| Approved Exswaping public-content API does not exist | Exswaping product/backend owner | Decide owner/repository/auth/fields/freshness/deployment phase and implement outside AutoStory |
| Origin TLS remains expired/mismatched from Phase 0 | Operations/TLS owner | Issue correct origin certificate and verify SAN/renewal before production promotion |
| Endpoint consumers remain unknown for direct Fastify access, public diagnostics, old admin clients, and opaque AutoStory routes | Product/service owners | Confirm consumers before removals or contract changes |
