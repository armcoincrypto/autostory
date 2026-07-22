# Envelope Specification

## Form
`enc:v1:aes256gcm:<key_id>:<urlsafe_b64_nonce>:<urlsafe_b64_ciphertext_and_tag>`

## Algorithm
AES-256-GCM via `cryptography.hazmat.primitives.ciphers.aead.AESGCM`

## Associated data
Default: `autostory:telegram-session:v1:accounts.session_string:<key_id>`

Optional account bind (tooling only, not compatible with EncryptedSessionText without context): append `:account:<id>` via contextvar.

## Detection rules
- `enc:v1:aes256gcm:` → v1 envelope
- any other `enc:` prefix → unknown envelope → **fail closed** (never treat as plaintext)
- non-`enc:` → legacy plaintext/path (allowed in disabled/transition only)
