# Deployment Plan (executed)

1. Build immutable release from pushed SHA `c9d1fe6`.
2. Verify manifest.
3. Backup systemd overrides.
4. Acquire `/var/lock/autostory-deploy/phase0-7.lock`.
5. Point scheduler → release singleton wrapper + mutation locks.
6. Point readiness → release script + encryption mode disabled (no migration).
7. Point web → same release; initially inherited prior AI loop env (incident); remediated to `AI_AGENT_AUTO_LOOP_ENABLED=false`.
8. Observe checkpoints.
