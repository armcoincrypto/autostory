# Domain and Runtime Map

## Hostname decision

`ex.zellotex.com` is the canonical evidenced hostname for the current Storyfleet/AutoStory operator application.

Evidence:

- Cloudflare DNS resolves `ex.zellotex.com`.
- Public TLS presents a valid wildcard certificate for `*.zellotex.com`.
- nginx declares `server_name ex.zellotex.com`.
- the route proxies to AutoStory on port 8000;
- application health identifies the service as Storyfleet;
- AutoStory operational documentation repeatedly names the Zellotex route.

Rejected candidates:

- `ex.zollotex.com`: no DNS result and no nginx/certificate/documentation ownership evidence.
- `ex.zollotex`: not a valid public DNS hostname and does not resolve.
- `zollotex.com`: does not resolve.

Important distinction: this is a hostname ownership finding, not authorization to cut over the route. The hostname is occupied by the application selected for reuse.

## Request path

```text
Public client
  -> Cloudflare DNS/TLS
  -> host nginx :443
  -> ex.zellotex.com server block
  -> Gunicorn 127.0.0.1:8000
  -> Flask/Jinja application
  -> SQLite /opt/autostory/data/storyfleet.db
  -> Telegram/OpenAI adapters when explicitly invoked
```

## Current logical modules

- accounts and Telegram sessions;
- readiness and health;
- discovery;
- Telegram stories;
- scheduling, templates, jobs, deliveries;
- Telegram gateway;
- negotiation-focused AI Agent;
- operator control and account governance;
- dashboard authentication;
- AI Coding external proxy.

## Runtime risks

- Cloudflare public TLS is healthy, but direct origin TLS is expired and hostname-incomplete.
- nginx global configuration still advertises legacy TLS 1.0/1.1 defaults, although Certbot include narrows configured TLS servers to 1.2/1.3.
- production code runs directly from `/opt/autostory`, not an immutable release directory.
- source, runtime data, uploaded media, backups, audit evidence, and environment files are colocated.
- systemd units and repository units have drift.
- scheduler and web processes share SQLite; prior corruption and current FK violations make broad schema evolution unsafe.
- no deployment lock or release manifest was found for AutoStory.

## Target runtime boundary

Retain a modular monolith and existing Python/Flask stack initially, but deploy from immutable releases:

```text
/opt/zollotex-social-agent/releases/<release-id>
/opt/zollotex-social-agent/current -> releases/<release-id>
/opt/zollotex-social-agent/shared/config
/opt/zollotex-social-agent/shared/media
/opt/zollotex-social-agent/shared/logs
```

These paths are a proposed post-gate layout. They were not created. Migration must preserve the current hostname owner and can use a separate loopback preview port before any nginx change.

Use dedicated names/prefixes:

- service: `zollotex-social-agent-web` and, only if retained separately, `zollotex-social-agent-worker`;
- database/schema: `zollotex_social_agent`;
- Redis/queue prefix: `zsa:`;
- session cookie: `zsa_session`;
- nginx upstream: `zollotex_social_agent`;
- media namespace: `zollotex-social-agent/`.
