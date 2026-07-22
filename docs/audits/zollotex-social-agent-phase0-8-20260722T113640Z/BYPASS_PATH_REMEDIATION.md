# Bypass Path Remediation

## Closed
- scanner / dashboard routes / healthcheck helper / fleet probe → `resolve_telethon_session`
- Legacy Fernet managers gated
- session_to_string export gated
- Static regression tests

## Residual risk (documented, not production-active for Social Agent)
- Bot UI paste-import and phone auth still accept session transport (operators; not enabled for publishing)
- Raw SQL migration tool intentionally bypasses ORM TypeDecorator (uses SessionMaterialService)

## Metric
Active account Telethon construction bypasses after remediation: **0** in guarded runtime paths.
