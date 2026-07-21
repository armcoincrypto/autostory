# Phase 0.5 Starting State

- Audit started: `2026-07-21T20:20:18Z`
- Canonical production repository: `/opt/autostory`
- Production branch: `claude/deploy-bot-vps-WbBG6`
- Production Git HEAD: `6f78eb91399562248e875e7da449226d3ee65c5d`
- Production worktree: dirty with unrelated tracked and untracked changes; preserved untouched
- Requested audit base branch: `feature/zollotex-social-agent-phase0-20260721`
- Requested audit base SHA: `017d32668e42fe03490640d179d9839d3324c7db`
- Base worktree: `/opt/zollotex-social-agent-phase0`, clean
- Phase 0.5 branch: `feature/zollotex-social-agent-phase0-5-remediation-20260722`
- Phase 0.5 worktree: `/opt/autostory-zollotex-phase0-5`
- Upstream: not configured for the new local branch

## Runtime identity

Production does not have a single authoritative runtime SHA because active processes import from the dirty `/opt/autostory` tree. The nearest Git identity is `6f78eb9`, but uncommitted source changes are runtime-visible.

| Process | State | PID | Start time | Restarts |
|---|---|---:|---|---:|
| `autostory-web` | active/running | 3031014 | 2026-07-18 15:50:03 CEST | 0 |
| `autostory-scheduler` | active/running | 703773 | 2026-07-14 22:51:51 CEST | 0 |
| `autostory-readiness-worker` | active/running | 2907594 | 2026-07-18 06:44:13 CEST | 0 |
| `telegram-gateway` | inactive/dead | 0 | none | 0 |
| `storyfleet-bot` | inactive/dead | 0 | none | 0 |
| `kathleen-account-listener` | inactive/dead | 0 | none | 0 |

## Database identity

- Path: `/opt/autostory/data/storyfleet.db`
- Owner/group: `storyfleet:storyfleet`
- Mode: `0644`
- Size at capture: `61,390,848` bytes
- Modification time at capture: `2026-07-21 21:43:22.229484461 +0200`
- Journal mode: WAL
- Structural quick check: `ok`
- Foreign-key violations: `910`

No service was restarted and no production database row was modified during identity capture.
