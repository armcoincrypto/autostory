# Rollback Rehearsal

| Scenario | Result |
|---|---|
| A return to transition (same release) | PASS — sed mode back + restart; encrypted DB compatible |
| B plaintext-only release against encrypted DB | BLOCKED / unsafe |
| C DB backup open | PASS — phase1-0 pre-encrypted-only backup quick/integrity ok |
| D key backup restore test | PASS — evidence backup file present 0600; load metadata without printing |

Preferred abort: restore `TELEGRAM_SESSION_ENCRYPTION_MODE=transition` on all three units.
