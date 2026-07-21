# Credential Rotation Matrix

No credential was rotated in Phase 0.5. Values are intentionally omitted.

| Category | Variable names | Exposure | Current use / runtime | Rotation method and order | Downtime / rollback | Verification / owner action |
|---|---|---|---|---|---|---|
| Telegram bot | `BOT_TOKEN` | Definitely exposed and still active | Bot integration; bot service presently inactive | Create replacement with BotFather; stage config; validate identity/scopes; restart only owning service; revoke old token after health check | Brief bot outage; rollback before revocation by restoring old token | Product/Telegram owner must approve bot identity and maintenance window |
| Telegram application credentials | `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` | Definitely exposed and still active | All Telethon user-session consumers | Inventory every session/client; obtain replacement application credentials if provider permits; stage both atomically; validate copied-session login in controlled test; then restart owning services | Potential fleet-wide authentication impact; rollback to old pair only while provider keeps it valid | Telegram account owner/provider-console action required |
| Dashboard/operator token | `DASHBOARD_ADMIN_TOKEN` | Definitely exposed and still active | Dashboard API/operator controls | Deploy header-only/timing-safe auth first; provision replacement to approved clients; update server; invalidate old token; inspect access logs | Short operator API interruption; dual-token grace only if explicitly implemented/reviewed | Operations owner must identify all consumers and verify old token rejection |
| Swaperex admin telemetry token | `ADMIN_API_TOKEN` | Definitely exposed during Phase 0.5 service metadata capture; value is omitted here | `/api/v1/admin/*` on the isolated Swaperex admin app and its operator SPA | Identify every operator client; remove build-time token fallback; provision replacement; update service/client session workflow; restart only admin app; reject old token | Brief admin telemetry interruption | Swaperex owner must rotate urgently and verify old token rejection |
| Dashboard session secret | `DASHBOARD_SECRET_KEY` or equivalent | Potentially exposed; older backup names do not prove current value | Flask sessions/CSRF | Rotate after operator token; expect all sessions to invalidate; restart web only | Operator re-login required; rollback recreates acceptance risk and is discouraged | Security owner schedules reauthentication |
| Database credentials | `DATABASE_URL`, `DB_PASSWORD`, `POSTGRES_PASSWORD` | Configuration definitely tracked; secret status depends on backend | Current AutoStory uses local SQLite URL; future/external DB unknown | Confirm whether URL embeds credentials; rotate database principal only if secret-bearing; update service before revoking old principal | Backend-dependent | Database owner must classify URL and dependent jobs |
| Redis credentials | `REDIS_PASSWORD` and Redis URL fields | Potentially exposed; active overlap includes host/port/db | Queue/cache configuration | Determine whether authentication exists; rotate ACL/password; update all workers atomically | Queue interruption possible | Runtime owner inventories producers/consumers |
| OpenAI/provider API keys | `OPENAI_API_KEY`, generic `*_API_KEY` | Present in newer untracked world-readable backups; potentially exposed | Optional AI/provider calls | Disable paid execution; create replacement; set spending limits; deploy; revoke old; inspect usage | No AI generation during rotation | Provider owner reviews usage/billing before and after |
| Telegram user sessions | `accounts.session_string`, session files | Plaintext-at-rest confirmed; not proven Git-tracked | 104 populated DB rows; filesystem sessions also exist | Do not revoke blindly; deploy transition encryption with key; migrate copy then production; separately revoke/re-auth only on compromise evidence | Revocation can lock out accounts | Telegram account owner approves any session revocation |
| Webhook/signing secrets | `*_WEBHOOK_SECRET`, `*_SIGNING_SECRET` | Potentially exposed where present in untracked backups | No complete active consumer map | Rotate provider-side with overlap window, deploy verifier, then retire old | Provider-specific | Integration owner required |
| Storage credentials | `AWS_*`, `S3_*`, `STORAGE_*` | Paths/config present; actual credential evidence unconfirmed | Local storage currently observed | Determine whether fields are paths or credentials; rotate IAM keys if applicable | Avoid media outage | Storage owner validates least privilege |
| RPC/explorer keys | RPC/explorer-specific API-key names | Potential exposure in Swaperex/other runtime, not proven in tracked AutoStory backups | Public proxy upstreams | Map upstream owner and quotas; rotate behind proxy; verify no key reaches clients/logs | Possible quote/explorer interruption | Swaperex owner action required |
| Social OAuth credentials | provider client/access/refresh tokens | No evidence in current AutoStory tracked backups | Future Social Agent | No rotation now; enforce encrypted storage before connection | none | Provider owner when integrations begin |

## Required order

1. Preserve evidence and finish consumer mapping.
2. Harden authentication and secret-file handling.
3. Rotate dashboard/operator access.
4. Rotate paid/provider keys.
5. Rotate bot/application credentials under Telegram-owner supervision.
6. Migrate session encryption; revoke individual user sessions only when separately approved.
7. Remove active-branch files, then decide on coordinated history rewrite.
8. Verify old credentials fail and scan all clones/releases/caches.
