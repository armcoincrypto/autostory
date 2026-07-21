# Credential Dependency Graph

| ID/category | Variables | Consumers | Rotation owner/order | Status |
|---|---|---|---|---|
| Swaperex admin | `ADMIN_API_TOKEN` | admin API, operator SPA | local security operator | rotated/revoked/pass |
| AutoStory operator | `DASHBOARD_ADMIN_TOKEN` | web API header clients | operations owner, then web | exposed; pending safe endpoint/client validation |
| Paid AI | `OPENAI_API_KEY`, provider keys | optional AI client | provider/billing owner | exposed/assumed; external replacement and usage review required |
| Telegram bot | `BOT_TOKEN` | inactive bot service | BotFather/product owner | exposed; external action required |
| Telegram app | `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` | scheduler/readiness/all Telethon clients | Telegram application owner | exposed; fleet-wide staged rotation required |
| Session signing | `DASHBOARD_SECRET_KEY` | Flask sessions/CSRF | security + operations | pending impact window; rotation invalidates sessions |
| Database | `DATABASE_URL` | web/scheduler/readiness | database owner | current local SQLite; classify embedded credentials before action |
| Redis | `REDIS_PASSWORD`/URL | queue/cache | runtime owner | password absent in active env inventory |
| Webhook/OAuth/RPC/storage | provider-specific names | integrations/Swaperex | external owners | classify provider dashboards; no invented values |
| Telegram user sessions | DB/session files | Telethon consumers | Telegram account owner | data, not rotated or migrated in this phase |

Dependency order is server replacement, client transition, scoped restart, acceptance test, old
revocation, old rejection test, and log/fingerprint scan. External-owner credentials remain
blocking and were not replaced with invented values.
