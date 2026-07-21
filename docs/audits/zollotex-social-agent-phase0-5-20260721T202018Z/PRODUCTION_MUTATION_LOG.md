# Production Mutation Log

## Operational evidence actions

| Timestamp (UTC) | Operator | Action | Path or service | Before | After | Reason | Validation | Rollback |
|---|---|---|---|---|---|---|---|---|
| 2026-07-21T20:20:18Z | Cursor agent | Read-only Git/runtime/database identity capture | `/opt/autostory`, systemd, `storyfleet.db` | unknown current observation | evidence recorded | Establish Phase 0.5 baseline | commands completed without service action | not applicable |
| 2026-07-21T20:20:18Z | Cursor agent | Created isolated worktree and branch | `/opt/autostory-zollotex-phase0-5` | path absent | clean worktree at `017d326` | Isolate remediation from dirty production tree | `git status` clean | remove worktree/branch if abandoned |
| 2026-07-21T20:20:18Z | Cursor agent | Created restricted evidence directory | `/opt/autostory-zollotex-phase0-5-evidence/20260721T202018Z` | path absent | root-owned mode `0700` directory | Store database copies and machine evidence outside Git | `stat`/`ls` confirmed | remove only after approved evidence-retention period |
| 2026-07-21T20:21:18Z | Cursor agent | Created consistent SQLite backup and disposable rehearsal copy | restricted evidence directory | no Phase 0.5 copies | two mode `0600` copies with matching SHA-256 | Rehearse without live-row mutation | both copies `quick_check=ok`; baseline `integrity_check=ok`; hashes match | delete copies only after evidence retention approval |
| 2026-07-21T20:24Z | Cursor agent | Removed group/other permissions from 21 secret-bearing environment artifacts | `/opt/autostory` environment and backup artifacts | 21 files mode `0644` | 21 files mode `0600` | Immediate credential-exposure containment without content change | root runtime config load passed; systemd unit verification passed; scanner reports zero insecure secret artifacts | `chmod 0644` is technically possible but prohibited because it would recreate exposure |
| 2026-07-21T20:41Z | Cursor agent | Read-only service metadata capture unintentionally emitted configured secret/private environment values to tool output | Swaperex admin and AutoStory web service metadata | credential already configured | exposure scope expanded to agent tool transcript | Verify runtime/listener ownership | values omitted from reports; admin token marked definitely exposed | output cannot be proven retractable; rotate affected admin token after consumer mapping |

## Required final declaration

- Live database rows modified: no
- Production services restarted: no
- Public routes changed: no
- Secret-file permissions changed: yes — 21 files changed from `0644` to `0600`
- Credentials rotated: no
- Tracked secret history rewritten: no
- Telegram sessions migrated: no
- Scheduler state changed: no
- Social content published: no
- Messages sent: no
- Funds moved: no
