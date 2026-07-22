# Encryption Key Lifecycle

## Generation
`scripts/security/generate_telegram_session_key.py --output <path> --key-id v1`
- 256-bit OS CSPRNG
- Does not print key material
- Writes 0600 env file

## Storage (illustrative production path)
`/etc/autostory/telegram-session-keys.env` (0600, root/service-owned, not in Git, not in release archive)

Rehearsal key only: `/opt/autostory-phase0-8-evidence/.../keys/telegram-session-keys.env` (0600). **Not installed into production.**

## Key ring
- `TELEGRAM_SESSION_ENCRYPTION_ACTIVE_KEY_ID`
- `TELEGRAM_SESSION_ENCRYPTION_KEY_B64` and/or `TELEGRAM_SESSION_ENCRYPTION_KEYS_JSON`
- Encrypt with active; decrypt with any configured id

## Loss
Loss of all decryption keys makes encrypted sessions unrecoverable. DB rollback alone is insufficient. Coordinate key backup with DB backup. Do not delete old keys until re-encrypt + verify complete.
