# Session Access Path Map

Goal: `DIRECT_SESSION_READS_OUTSIDE_CANONICAL_BOUNDARY=0` for account Telethon construction.

## Canonical
- `SessionMaterialService` / `EncryptedSessionText` — DB crypto
- `resolve_telethon_session` — Telethon session object construction

## Remediated this phase
| ENTRY | BEFORE | AFTER |
|---|---|---|
| discovery/scanner get_active_client | ad-hoc StringSession/SQLiteSession | resolve |
| dashboard fleet health / story precheck | ad-hoc | resolve |
| manager._existing_session_source_for_healthcheck | direct path/StringSession | resolve |
| fleet_certification.probe_account_telegram | direct StringSession | resolve (+ temp SQLite copy for file) |
| core/session_manager.get_client | Fernet bypass | RuntimeError unless legacy flag |
| clients/session.sync_to_database | Fernet→plaintext DB | RuntimeError unless legacy flag |
| scripts/session_to_string.py | stdout export | requires ACKNOWLEDGE_SESSION_STRING_EXPORT=1 |

## Remaining intentional exceptions
| ENTRY | R/W | NOTES |
|---|---|---|
| ClientManager import/phone/QR | W | ORM EncryptedSessionText; StringSession for auth validation only |
| tdata_convert | convert | offline import feed |
| migrate_telegram_session_encryption.py | R/W raw SQL | ops tool; uses SessionMaterialService |
| bot bot-token SQLiteSession | bot rail | not account vault |

## Test coverage
`tests/test_telegram_session_bypass_guard.py`, encryption suite.
