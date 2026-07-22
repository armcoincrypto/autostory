# Contributing Factors

1. Concurrent Phase 0.6 canary during Phase 1.0 promote window without shared mutation freeze.
2. Safety reports sampled post-cleanup env, not execution-time process env.
3. `CONTROLLED_STORY_EXECUTION_ENABLED` alone authorized live publish (no independent global deny).
4. Systemd drop-in lexical ordering caused one failed attempt before successful enable.
5. Direct gunicorn POST bypassed nginx access logs, reducing visibility.
6. No short-lived provider authorization token at Telegram adapter boundary.
7. No account mutation allowlist independent of controlled account id flag.
