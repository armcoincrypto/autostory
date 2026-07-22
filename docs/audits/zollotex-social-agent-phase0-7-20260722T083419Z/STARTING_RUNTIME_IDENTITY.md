# Starting Runtime Identity

Capture UTC: `20260722T083419Z`

## Repository

| Field | Value |
|---|---|
| Path | `/opt/autostory` |
| Branch | `claude/deploy-bot-vps-WbBG6` |
| HEAD | `499758e3465af1908fe31e197b0bd6e0e136b6ef` |
| Upstream | `45890237a6f86fd47eeb08fb28a0fb1bfb088534` |
| Worktree | Heavily dirty (modified tracked + large untracked set) |

## Candidate

| Field | Value |
|---|---|
| Branch | `fix/autostory-canonical-runtime-lineage-20260722` |
| SHA | `24e77d520121fc2394d4fe4c2d0d5a3f60320abe` |
| Phase 0.7 work branch | `fix/autostory-phase0-7-regression-runtime-promotion-20260722` |
| Work tree | `/opt/autostory-phase0-7-work` |
| Certification checkout | `/opt/autostory-phase0-7-certification` |

## Production processes (observed)

| SERVICE | PID | START_TIME (CEST) | RESTART_COUNT | WORKING_DIRECTORY | LOADED ENTRY | OBSERVED_SHA | MUTATION |
|---|---|---|---|---|---|---|---|
| autostory-web | 479280 (worker 479290) | 2026-07-22 06:58:03 | 0 | `/opt/autostory-releases/20260721T232351Z-499758e3465a` | gunicorn `wsgi:app` | `499758e` | read-serving |
| autostory-scheduler | 479281 | 2026-07-22 06:58:03 | 0 | `/opt/autostory` (dirty) | `main.py scheduler` | dirty worktree | locked via env |
| autostory-readiness-worker | 479277 | 2026-07-22 06:58:03 | 0 | `/opt/autostory` (dirty) | `scripts/run_readiness_worker.py` | dirty worktree | probe-only expected |
| telegram-gateway | inactive | — | 0 | `/opt/autostory` | `src.telegram_gateway.worker` | n/a | stopped |
| storyfleet-bot | inactive | — | 0 | `/opt/autostory` | `main.py bot` | n/a | stopped |
| kathleen-account-listener | inactive | — | 0 | `/opt/autostory` | `src.bot.kathleen_account_listener` | n/a | stopped |
| swaperex-admin | 479257 | 2026-07-22 06:58:02 | 0 | `/root/Swaperex` | uvicorn admin | separate | dependency awareness only |

## Safety flag status (from `/opt/autostory/.env`, values non-secret)

```text
SCHEDULER_MUTATIONS_ENABLED=false
STORY_EXECUTION_ENABLED=false
CAMPAIGN_EXECUTION_ENABLED=false
DISCOVERY_EXECUTION_ENABLED=false
AI_CODING_EXECUTE_ENABLED=false
EXECUTION_EMERGENCY_LOCK=false
AI_AGENT_AUTO_LOOP_ENABLED=missing
READINESS_WORKER_ENABLED=missing
TELEGRAM_GATEWAY_ACTIVE=missing
TELEGRAM_BOT_ACTIVE=missing
KATHLEEN_LISTENER_ACTIVE=missing
SOCIAL_PUBLISHING_ENABLED=missing
AI_AUTOPUBLISH_ENABLED=missing
ENVIRONMENT=production
```

Process state corroboration: gateway/bot/Kathleen inactive; scheduler/readiness active from dirty tree; web on immutable release.

## Shared references (web release)

- data: `/opt/autostory/data`
- env: `/opt/autostory/.env`
- venv: `/opt/autostory/venv`
