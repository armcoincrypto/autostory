# Production Mutation Log

| UTC timestamp | Action | Service/path | Credential category | Before (redacted) | After (redacted) | Process restarted | Old credential revoked | Validation | Rollback available | Operator |
|---|---|---|---|---|---|---|---|---|---|---|
| 2026-07-21T23:04:10Z | Read-only Git/runtime/listener capture | `/opt/autostory`, systemd, `/proc`, listeners | none | unknown current observation | evidence recorded | no | n/a | commands completed | n/a | Cursor agent |
| 2026-07-21T23:04:10Z | Created isolated candidate worktree | `/opt/autostory-canonical-phase0-6` | none | path absent | clean branch at `0259f9a` | no | n/a | `git status` clean | remove worktree/branch if abandoned | Cursor agent |
| 2026-07-21T23:04:10Z | Created restricted evidence directory | `/opt/autostory-phase0-6-evidence/20260721T230410Z` | none | path absent | root-owned mode `0700` | no | n/a | directory metadata verified | remove after approved retention period | Cursor agent |
| 2026-07-21T23:07:10Z | Rotated exposed Swaperex administrator token; moved secret out of world-readable drop-in | `swaperex-admin.service`, `/etc/swaperex/swaperex-admin.env`, systemd drop-in | Swaperex admin/operator token | inline token in mode-`0644` drop-in; old token accepted | replacement in root-only mode-`0600` EnvironmentFile; old token removed | yes, only `swaperex-admin.service` | yes (removed from active configuration) | new=200, old=401, missing=401; service active; neither token in journal | restricted old drop-in preserved, but restoring compromised token is emergency-only | Cursor agent |
| 2026-07-21T23:24:37Z | Externally observed web promotion and restart; not performed by this phase | `autostory-web.service`, release `20260721T232351Z-499758e3465a` | none | web previously loaded `/opt/autostory` | web loads immutable `git archive` release at `499758e` | yes, by another operator | n/a | service active; release manifest inspected | external operator responsibility | unknown/concurrent operator |

## Final declaration

- Production application code deployed: yes — externally by another operator; Phase 0.6 candidate not deployed
- Production database rows changed: no
- Production services restarted: yes — `swaperex-admin.service` by this phase; AutoStory web externally
- Credentials rotated: yes — Swaperex administrator token
- Old credentials revoked: yes — removed from active config and rejected
- Tracked backups removed from active branch: no
- Git history rewritten: no
- Telegram sessions migrated: no
- Scheduler activation changed: no
- Social accounts connected: no
- Social content published: no
- Messages sent: no
- Funds moved: no
