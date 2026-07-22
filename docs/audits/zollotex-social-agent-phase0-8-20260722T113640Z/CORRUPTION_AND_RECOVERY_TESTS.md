# Corruption and Recovery Tests

| Case | Result |
|---|---|
| truncated envelope | rejected |
| unknown version / enc: prefix | rejected (fail closed) |
| wrong key | rejected |
| missing key ring | rejected |
| modified ciphertext | rejected |
| plaintext in encrypted-only | rejected |
| double encryption | idempotent (no nest) |
| legacy in transition | allowed |
