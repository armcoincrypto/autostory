# Current State

## Repository lineage

### AutoStory / Storyfleet

- Production path: `/opt/autostory`
- Remote: `git@github.com:armcoincrypto/autostory.git`
- Branch: `claude/deploy-bot-vps-WbBG6`
- Local HEAD: `6f78eb91399562248e875e7da449226d3ee65c5d`
- Upstream: `origin/claude/deploy-bot-vps-WbBG6`
- Upstream SHA: `45890237a6f86fd47eeb08fb28a0fb1bfb088534`
- Divergence: 40 commits ahead, 0 behind
- Worktree: dirty; 35 tracked files changed plus extensive untracked source tests, audit data, backups, and runtime artifacts
- Production release SHA: indeterminate because processes import directly from the dirty worktree

### Swaperex / Kobbex DEX

- Repository path: `/root/Swaperex`
- Remote: `git@github.com:armcoincrypto/Swaperex.git`
- Branch: `release/kobbex-dex-brand-unification`
- Local HEAD: `bc05d70ac803bca30d0ab0f5fc4611be1c3d6a0d`
- Matching remote branch SHA: `38ed09d57ea5f65140e6a605b13c35685320339c`
- No upstream configured; branch is 25 commits ahead of `origin/main`
- Worktree: dirty with tracked and untracked audit/config artifacts
- Current product identity: Kobbex non-custodial DEX at `dex.kobbex.com`; older “Exswaping” custodial/API documentation is explicitly marked obsolete in its README

## Application stack

- Backend/UI: Python, Flask, Jinja templates, JavaScript, Gunicorn
- ORM: SQLAlchemy
- Live database: SQLite WAL at `/opt/autostory/data/storyfleet.db`
- Queue/scheduler: persisted SQLite jobs plus systemd scheduler; Telegram gateway queue is present but its worker is disabled
- Redis: host Redis 7.0.15 is running, but the certified production publishing path is not a demonstrated Celery deployment
- AI: OpenAI-backed draft generation for Telegram negotiation tasks
- Telegram: Telethon user-account sessions, stories, channel/group scheduling, readiness, gateway, and delivery records
- Media: persistent files under `/opt/autostory/data/media`; nginx body limit is 50 MiB
- Schema management: startup-time `create_all()` and ad hoc `ALTER TABLE`; Alembic is declared but no migration tree was found

## Runtime

- Hostname: `ex.zellotex.com`
- nginx upstream: `127.0.0.1:8000`
- `autostory-web`: active, enabled, Gunicorn single worker
- `autostory-scheduler`: active, enabled
- `autostory-readiness-worker`: active, enabled
- `autostory-readiness-soak-checkpoint.timer`: active, enabled
- `telegram-gateway`: inactive, disabled
- `storyfleet-bot`: inactive, disabled
- `kathleen-account-listener`: inactive, disabled

The effective systemd units include local drop-ins not represented entirely by repository unit files. Some effective units run as root. The web drop-in contains account identifiers/phone metadata directly in unit configuration, which is an operational information-exposure risk.

## Secrets and session material

- `/opt/autostory/.env` is mode `0644`.
- Three `.env.bak.2026-03-15_*` files are mode `0644` and tracked by Git.
- Additional untracked `.env.backup.*` and phase backup files are present.
- The `accounts.session_string` column is plain SQLAlchemy `Text`; a source comment calls it encrypted, but universal encryption/decryption enforcement was not found.
- Environment or Telegram session values were not read or printed during discovery.

## Authentication and authorization

- Local username/password login, password hashes via Werkzeug.
- Flask session cookies and CSRF protection are present.
- Dashboard users have only `is_admin` and `is_active`; no Owner/Admin/Editor/Approver/Analyst/Viewer model.
- JSON APIs can also accept a shared `X-Admin-Token`.
- Current API authorization accepts authenticated users where `is_admin` is null; this legacy compatibility behavior is too permissive for the target platform.
- Telegram account “roles” are runtime account-governance labels, not human operator RBAC.

## Production safety state

- `AUTOSTORY_PRODUCTION_CERTIFIED_NO_GO=true`
- `PROMO_GENERATION_MODE=disabled`
- `SCHEDULER_MUTATIONS_ENABLED=false`
- `STORY_EXECUTION_ENABLED=false`
- `CAMPAIGN_EXECUTION_ENABLED=false`
- `DISCOVERY_EXECUTION_ENABLED=false`
- `AI_CODING_EXECUTE_ENABLED=false`
- `P6_4_SINGLE_SEND_ENABLED=false`

No existing live-send capability is authorized for use by this program.
