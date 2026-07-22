# Open Blockers

1. **Owner: ops** — authorize production Telegram session migration (separate phase).
2. **Owner: ops** — install production key ring at approved path and verify backup.
3. **Owner: ops** — after migration, retire active FS `.session` files and backup copies under retention policy.
4. **Owner: product** — bot paste-import / phone auth transport remains high-risk if re-enabled; keep disabled for Social Agent.
5. **Owner: data** — 910 FK violations remain (separate from encryption).

6. **Owner: ops (URGENT)** — production SHA split: web+readiness=`9b3ccc0`, scheduler=`c9d1fe6`. Restore single immutable SHA across all three before any Telegram encryption migration.
