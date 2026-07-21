# Telegram Session Material Path Map

## Storage formats

1. `accounts.session_string`: plaintext Telethon StringSession or legacy path text.
2. `accounts.session_path`: dedicated SQLite-session path.
3. Canonical `data/sessions/account_<id>.session`: plaintext Telethon SQLite auth-key store.
4. QR staging `account_<id>.new.session`.
5. Bot `data/bot/storyfleet_bot.session`.
6. Redis `session:<id>` Fernet value with seven-day TTL.
7. Encrypted JSON `session_<id>.json` using a separate `.key`; appears dormant.
8. Whole-database, recovery-lab, repair, and `.bak.*` copies containing plaintext DB/session material.

## Producer classes (9)

| Entry point | Call chain / write | Encryption today | Error/exposure behavior |
|---|---|---|---|
| Phone login API/UI | auth start/complete → `ClientManager` → `StringSession.save()` → `Account.session_string` | none | temporary StringSession returned to browser and resubmitted |
| StringSession import API | `/api/accounts/import-session` → manager validation → insert/update account | none | import errors may include raw exception text; source/bytecode signature drift exists |
| TDATA/session conversion | extraction/conversion → StringSession candidates → import manager | none | auth-key material exists in temporary extraction |
| New-account QR | manager daemon thread → QR login → `session.save()` → account | none | process-global QR URL/status cache has no proven expiry cleanup |
| QR repair | SQLite `account_<id>.new.session` → identity verification → install utility | none | absolute paths returned to operator UI |
| Telegram bot phone login | bot pending login → code/2FA → `_save_account` | none | live client/code hash retained in process memory |
| Telegram bot paste import | operator sends StringSession in Telegram message → import manager | none | secret persists in Telegram message history |
| CLI phone login | `main.py add-account` → manager → DB | none | terminal/process exposure possible |
| Encrypted JSON sync | `clients/session.py` decrypts JSON then `sync_to_database` | encrypted file, plaintext DB write | separate unmanaged key/format; dormant-looking rail |

`scripts/session_to_string.py` is an export path that prints full session material to stdout and must be disabled or converted to an explicitly guarded secure handoff.

## Consumer classes (14)

| Consumer | Read/resolve path | Central resolver used | Current risk |
|---|---|---|---|
| `clients/session_resolve.py` | path text → `session_path` → canonical file → StringSession | canonical boundary | no encryption decode today |
| `ClientManager` connect/publish/readiness | account → resolver or direct helper | mixed | some direct `StringSession` construction |
| Dedicated readiness worker | account → manager/resolver → Telegram authorization probe | yes | external reads and DB readiness writes |
| Scheduler executor | account/session → direct or gateway Telegram execution | mostly manager | can send when gates allow |
| Membership/joiner | account → manager/resolver | yes | Telegram external actions |
| AI direct transport | account → manager/resolver | yes | can fetch/send |
| Kathleen listener | account lock/resolver | yes with special preference | long-lived session and outbound replies |
| Story publisher | direct account read → `StringSession` | bypass in portions | ciphertext/path misclassification risk without ORM decode |
| Discovery scanner | raw `session_string` path/StringSession branch | no | ignores dedicated/canonical paths |
| Fleet certification | direct StringSession or temporary SQLite copy | no | duplicates sensitive file |
| Core SessionManager/health monitor | raw DB fallback → direct StringSession | no | Redis encryption fails open to plaintext |
| Dashboard fleet health source | direct account/session use | mixed/bytecode | active route behavior partly opaque |
| Recovery/audit/install scripts | direct file/DB copy, repair, conversion | no | many plaintext backup classes |
| Browser/API continuation | temporary auth session and QR capability | n/a | capability material crosses frontend boundary |

## Existing encryption helpers

- `core/session_manager.py`: Fernet Redis cache using `data/session.key`; silently falls back to plaintext bytes on encryption failure and plaintext DB on decrypt failure.
- `clients/session.py`: Fernet-encrypted JSON using `<sessions_dir>/.key`; synchronization writes plaintext to the DB.

These helpers use different keys/formats, lack key IDs/rotation, and are not the canonical production DB boundary.

## Phase 0.5 implementation coverage

`EncryptedSessionText` now centralizes normal SQLAlchemy reads/writes of `accounts.session_string`. In transition mode it encrypts writes and decrypts both encrypted and legacy plaintext reads, so existing ORM consumers receive the legacy in-memory form. Recognized encrypted envelopes fail closed on missing/wrong keys or tampering.

Coverage limitations:

- raw SQL can bypass the type;
- SQLite session files, bot sessions, repair copies, and whole-DB backups remain separate plaintext-at-rest risks;
- path strings remain mixed with secret strings in one column;
- direct consumers still bypass the canonical resolver;
- browser auth continuation and bot paste import still transport secret capability material;
- archived bytecode route ownership prevents complete source proof;
- the two Fernet managers are not yet retired/fixed.

Therefore the DB encryption rehearsal is successful, but full Telegram session-path centralization is not production-certified.
