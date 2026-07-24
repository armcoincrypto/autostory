# Architecture — Social Agent on ex.zellotex.com

Verdict: `EX_ZELLOTEX_SOCIAL_AGENT_DISCOVERY_PASS`

## Ownership

`https://ex.zellotex.com` is served by STORYFLEET / AutoStory (`armcoincrypto/autostory`):

- nginx `sites-available/autostory` → `127.0.0.1:8000`
- systemd `autostory-web` → immutable release under `/opt/autostory-releases/`
- Flask + Jinja + Bootstrap shell (`base.html`)
- SQLite shared at `/opt/autostory/data/storyfleet.db`
- Auth: Flask-Login + `X-Admin-Token`

There is no separate multi-agent SPA. Social Agent is a first-class workspace inside this product.

## Agent registry

Canonical thin registry: `src/social_agent/registry.py`

Agents: AI Agent, AI Coding, Broadcast, Social Agent.

Launcher UI: `/agents`. Workspace: `/social-agent/*`.

## Canonical service invariant

UI actions, AI chat tool calls, and API requests all call `src/social_agent/services.py`.

Tool definitions live in `src/social_agent/tools.py`. Live `publishing.publish` / `publishing.schedule` stay unavailable until canary authorization.

## Integrations (honest status)

| Provider | Status |
|---|---|
| Meta | OAuth scaffolding; credentials optional env |
| Telegram | Narrow AutoStory adapter status; live Story fail-closed |
| X / LinkedIn / Discord / YouTube / TikTok | NOT_STARTED |
| Exswaping | NOT_CONFIGURED (official API required) |

## Deployment

Immutable `git archive` releases via `scripts/release/build_immutable_release.sh`.
Rollback: previous `/opt/autostory-releases/*` + systemd WorkingDirectory/PYTHONPATH.
