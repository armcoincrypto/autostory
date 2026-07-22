# Containment Actions

```
GLOBAL_STORY_MUTATION_KILL_SWITCH=deny (STORY_MUTATIONS_ENABLED=false)
STORY_EXECUTION_MODE=disabled
STORY_ACCOUNT_MUTATION_ALLOWLIST= (empty deny)
CONTROLLED_STORY_EXECUTION_ENABLED=false
SCHEDULER_MUTATIONS_ENABLED=false
ALL_STORY_CANARY_EXECUTION_DISABLED=true (steady state)
ALL_DIRECT_PROVIDER_STORY_CALLS_BLOCKED=true without valid token + global on
```

Readiness session reads preserved. Sessions not revoked. Quarantine/backups retained.
