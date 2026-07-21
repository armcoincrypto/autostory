# Tracked Secret Removal

Removed from the candidate:

- `.env.bak.2026-03-15_011640`
- `.env.bak.2026-03-15_015848`
- `.env.bak.2026-03-15_020005`

All were introduced by `99b04be8152419e24bfbea4e570f584f95a2e6c7`, were remotely exposed,
and are not active runtime dependencies. Active `/opt/autostory/.env` was not deleted.

Controls: precise `.gitignore` patterns; placeholder-only `.env.example`; permission validator;
redacting tracked-path/content scanner; six focused tests; GitHub Actions enforcement. Validation
scanned 450 tracked files with zero findings. Deletion from the candidate does not remove history.
