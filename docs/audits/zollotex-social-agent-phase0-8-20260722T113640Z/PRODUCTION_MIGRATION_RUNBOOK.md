# Production Migration Runbook (DO NOT EXECUTE WITHOUT AUTHORIZATION)

## Preconditions
- Immutable runtime SHA verified
- Access paths centralized
- Active key installed at approved path (0600) + key backup verified
- Consistent production DB backup verified
- Gateway/bot/Kathleen stopped; AI loop disabled; scheduler mutation locked
- No concurrent deploy / account-add
- Expected counts captured (currently 104/104 plaintext)

## Order
1. Deploy transition-capable code if not live (keep mode `disabled` initially)
2. Validate key visibility (safe metadata only)
3. Set mode `transition`; restart web/scheduler/readiness together
4. Verify health + mixed-mode reads
5. Pause session-writing operations
6. Consistent backup
7. Dry-run with expected counts + `--materialize-filesystem-sessions`
8. Execute migrate with `--allow-production --acknowledge-production-migration`
9. Verify all rows; client local construction sample
10. Observe; retain `transition`
11. Separate gate for `encrypted-only` and FS retirement

## Abort
Unexpected counts, unknown format, missing key, decrypt failure, backup failure, concurrent write, lock anomaly, health degradation, any Telegram mutation.
