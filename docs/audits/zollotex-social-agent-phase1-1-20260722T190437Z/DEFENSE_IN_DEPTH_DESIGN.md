# Defense in Depth

1. Global `STORY_MUTATIONS_ENABLED` fail-closed
2. `STORY_EXECUTION_MODE` enum
3. Account allowlist default empty deny
4. Trigger authorization
5. Provider HMAC single-use token at `invoke_send_story`
6. Invalidate-on-disable / consume-time recheck
7. Gateway/bot/Kathleen/AI remain stopped
