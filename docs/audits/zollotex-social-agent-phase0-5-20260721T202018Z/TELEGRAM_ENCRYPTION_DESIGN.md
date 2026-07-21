# Telegram Session Encryption Design

## Central boundary

`src/security/session_material.py` defines `SessionMaterialService` and the SQLAlchemy `EncryptedSessionText` type. `Account.session_string` uses that type, so normal ORM producers and consumers pass through one storage boundary without changing business call sites.

Direct raw SQL remains prohibited for session material. The migration utility is the only reviewed exception and handles ciphertext explicitly.

## Envelope

```text
enc:v1:aes256gcm:<key-id>:<base64url-nonce>:<base64url-ciphertext-and-tag>
```

- Algorithm: AES-256-GCM from `cryptography`
- Nonce: random 96-bit nonce for every encryption
- Authentication context: `autostory:telegram-session:v1:<key-id>`
- Authentication tag: managed and verified by AES-GCM
- Keys: environment/approved secret manager only; never database
- Rotation: envelopes carry a key ID; reads accept configured historical keys; writes use the active key

## Configuration

- `TELEGRAM_SESSION_ENCRYPTION_MODE=disabled|transition|encrypted-only`
- `TELEGRAM_SESSION_ENCRYPTION_ACTIVE_KEY_ID`
- `TELEGRAM_SESSION_ENCRYPTION_KEY_B64` for one active key, or
- `TELEGRAM_SESSION_ENCRYPTION_KEYS_JSON` for a key-ID map during rotation

`disabled` preserves current development behavior. When `ENVIRONMENT=production`, an omitted mode defaults to `transition`, so a missing active key fails closed. `transition` reads legacy plaintext and writes encrypted values. `encrypted-only` rejects plaintext reads. Production promotion must fail unless transition/encrypted-only mode has a valid 32-byte active key.

An `enc:` value never falls back to plaintext. Unknown key IDs, malformed envelopes, wrong keys, and modified ciphertext fail with redacted errors.

## Staged rollout

1. Add and test the boundary; keep deployment blocked.
2. Provision a versioned key in approved secret storage.
3. Deploy `transition` mode: legacy reads allowed, all ORM writes encrypted.
4. Take a consistent database backup and run count-guarded migration under a writer-quiescence plan.
5. Verify every ciphertext decrypts, application reads work, and no plaintext remains.
6. Observe a defined soak period and retain legacy read metrics.
7. Change to `encrypted-only` only after owner approval.
8. Rotate by adding a new key ID, switching the active ID, migrating old envelopes, then retiring the old key after backup-retention review.

Production session rows were not changed in Phase 0.5.
