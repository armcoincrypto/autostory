# Open Blockers

| Blocker | Owner | Required action |
|---|---|---|
| Kathleen authoritative source missing | Product owner | Recover `.py` lineage or formally retire |
| Telegram session encryption production migration | Security/runtime owner | Approve key material + migration rehearsal before enabling transition/encrypted-only |
| Remaining credential rotations (dashboard token, providers) | Security owner | Rotate with protected validation endpoints |
| FK production repair | DB owner | Apply only after approved rehearsal cutover |
| Dirty `/opt/autostory` worktree still exists | Operators | Do not run services from it; clean up separately |
| Follow-up commit `728ab74` (docs/script) not in live release | Release owner | Optional rebuild/promote when convenient; live release remains `c9d1fe6` |
