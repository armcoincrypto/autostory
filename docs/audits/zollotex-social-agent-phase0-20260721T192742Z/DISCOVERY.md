# Zollotex Social AI Agent — Phase 0 Discovery

- Audit timestamp: `2026-07-21T19:27:42Z`
- Evidence host: `207.180.212.142`
- Audit mode: read-only infrastructure and production inspection; isolated documentation worktree only
- Canonical existing application repository: `/opt/autostory`
- Isolated audit worktree: `/opt/zollotex-social-agent-phase0`
- Audit branch/SHA: `feature/zollotex-social-agent-phase0-20260721` / `6f78eb91399562248e875e7da449226d3ee65c5d`

## Verdict

`ZOLLOTEX_SOCIAL_AGENT_PHASE_0_DISCOVERY_BLOCKED_LINEAGE_TLS_DATA_INTEGRITY`

The hostname spelling is resolved: `ex.zellotex.com` is the evidenced canonical hostname for the existing Storyfleet/AutoStory operator product. It has live DNS, a Cloudflare edge certificate for `*.zellotex.com`, an nginx server block, and a healthy Storyfleet response. `ex.zollotex.com`, `zollotex.com`, and `ex.zollotex` do not resolve.

The hostname is already owned by AutoStory and must not be reassigned. The Social AI Agent should evolve the existing application behind that hostname only after lineage certification, or use a separately approved hostname for an isolated preview. No routing was changed.

## Evidence collected

- DNS: `ex.zellotex.com` resolves through Cloudflare; misspelled candidates do not resolve.
- Public TLS: valid `*.zellotex.com` edge certificate, valid 2026-07-09 through 2026-10-07.
- Origin TLS: nginx serves an expired certificate (expired 2026-06-11) whose SANs omit `ex.zellotex.com`.
- Reverse proxy: `/etc/nginx/sites-enabled/autostory` proxies `ex.zellotex.com` to `127.0.0.1:8000`.
- Public route: `/` redirects to login; `/api/health` reports `{"service":"storyfleet","status":"healthy"}`.
- Runtime: systemd web, scheduler, readiness worker, and readiness checkpoint timer are active; Telegram gateway, Storyfleet bot, and Kathleen listener are inactive.
- Storage: production uses `/opt/autostory/data/storyfleet.db` SQLite WAL, not the PostgreSQL/Celery topology described by the generic Docker Compose file.
- Database: `PRAGMA quick_check=ok`; `PRAGMA foreign_key_check` reports 910 violations.
- Safety flags: production NO_GO is true; scheduler, story, campaign, discovery, AI Coding, and P6.4 live mutations are disabled.
- AI: OpenAI configuration and a Telegram negotiation-oriented AI Agent exist; there is no general typed Social AI tool registry.
- Authentication: Flask-Login local users with one `is_admin` boolean and an optional shared admin API token; no workspace RBAC.
- Existing providers: Telegram only. No certified Meta, X, LinkedIn, Discord, YouTube, or TikTok adapters were found.
- Exswaping evidence: `/root/Swaperex` is now a Kobbex-branded non-custodial DEX repository. It does not expose the requested public business-content API for exchange rates, directions, reserves, promotions, news, blog, referrals, or approved company facts.

## Safety actions

- No production file, database, service, DNS record, nginx configuration, certificate, or social account was changed.
- No external message or post was sent.
- No production API requiring credentials was called.
- A clean isolated Git worktree was created for audit documents only.

## Immediate blockers

1. Production executes from a dirty worktree with 35 tracked source modifications and extensive untracked runtime/audit artifacts.
2. Local AutoStory HEAD is 40 commits ahead of its upstream; upstream is at `4589023`, local HEAD at `6f78eb9`.
3. Runtime source therefore cannot be represented by one immutable release SHA.
4. Origin TLS is expired and does not cover `ex.zellotex.com`.
5. The live SQLite database has 910 foreign-key violations and a recent corruption recovery history.
6. Production scheduler is active despite older documentation describing it as stopped; runtime truth must supersede stale audit prose.
7. There is no approved Exswaping public-content API contract.
8. Current authentication and authorization do not meet the required role/workspace model.

Phase 1 production promotion is prohibited until these blockers are closed. Isolated implementation may continue in a clean worktree after the target baseline is explicitly certified.
