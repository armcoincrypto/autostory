# Migration Tool Design

Command: `scripts/db/migrate_telegram_session_encryption.py`

Modes: `inspect | dry-run | migrate | verify | reencrypt | rollback-check`

Guards:
- dry-run/inspect default for analysis
- refuses `/opt/autostory/data/storyfleet.db` without `--allow-production`
- mutate requires `--acknowledge-disposable-copy` or `--acknowledge-production-migration`
- expected count guards
- requires `TELEGRAM_SESSION_ENCRYPTION_MODE=transition` for migrate
- `--materialize-filesystem-sessions` converts Telethon `.session` files to StringSession before encrypt
- no session material in output
