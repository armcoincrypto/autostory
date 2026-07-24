# Discovery — ex.zellotex.com Social Agent ownership

## Discovery table

| Field | Value |
|---|---|
| HOSTNAME | `ex.zellotex.com` |
| NGINX_SERVER_BLOCK | `/etc/nginx/sites-available/autostory` (enabled) |
| UPSTREAM | `http://127.0.0.1:8000` (gunicorn) |
| SERVICE | `autostory-web.service` |
| FRONTEND_REPOSITORY | `github.com:armcoincrypto/autostory` (Jinja/Flask templates) |
| BACKEND_REPOSITORY | same (`armcoincrypto/autostory`) |
| PRODUCTION_RELEASE | `/opt/autostory-releases/20260722T201852Z-c862d9f8f8c0` |
| PRODUCTION_SHA | `c862d9f8f8c06bad64e390bd3ca43c06ab93a467` |
| FRAMEWORK | Flask + Jinja + Bootstrap 5 (server-rendered) |
| DATABASE | SQLite `/opt/autostory/data/storyfleet.db` |
| AUTHORITY | STORYFLEET / AutoStory ops dashboard (Cloudflare → nginx → gunicorn) |
| AGENT_REGISTRY | **None as product registry** — flat sidebar workspaces (AI Agent, AI Coding, Broadcast) |
| DEPLOYMENT_METHOD | Immutable `git archive` releases under `/opt/autostory-releases/` |
| ROLLBACK_TARGET | `/opt/autostory-releases/20260722T201529Z-7b38c58be7b5` |

## Discovery verdict

```text
EX_ZELLOTEX_OWNER_IDENTIFIED=true
EXISTING_AGENT_REGISTRY_IDENTIFIED=false
FRONTEND_REPOSITORY_IDENTIFIED=true
BACKEND_REPOSITORY_IDENTIFIED=true
AUTHORITY_IDENTIFIED=true
DEPLOYMENT_LINEAGE_IDENTIFIED=true
ROLLBACK_IDENTIFIED=true
```

Note: No separate multi-agent product exists at this hostname. Social Agent will be introduced as a **first-class workspace** plus a thin **Agents** launcher registry (AI Agent, AI Coding, Broadcast, Social Agent) without rebranding Stories/Accounts admin as Social Agent. AutoStory Telegram capabilities remain behind a narrow publishing adapter.

## Existing agent-like inventory

| AGENT_ID | DISPLAY_NAME | ROUTE | STATUS |
|---|---|---|---|
| ai_agent | AI Agent | `/ai-agent` (HTML route gap); `/api/v1/ai-agent/*` | API present; auto-loop disabled |
| ai_coding | AI Coding | `/ai-coding` | UI present; execute disabled |
| broadcast | Broadcast | `/broadcast` | Present |
| kathleen | Kathleen | systemd | inactive |
| storyfleet_bot | Telegram Bot | systemd | inactive |
| social_agent | Social Agent | `/social-agent` + `/agents` | ACTIVE (this release) |

## Architecture pass

See `ARCHITECTURE.md`.

```text
EX_ZELLOTEX_SOCIAL_AGENT_DISCOVERY_PASS
```
