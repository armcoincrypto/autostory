# Transition-Mode Soak

| Metric | Value |
|---|---|
| Migration complete (Phase 0.9) | ~20260722T124806Z |
| Soak elapsed at Phase 1.0 start | ~74.6 minutes (+ continuing) |
| Readiness cycles | 69 |
| Accounts checked (sum) | 477 |
| Ready (sum) | 465 |
| session_errors | 0 |
| Decrypt failures observed | 0 |
| Missing-key failures | 0 |
| Filesystem fallbacks / open .session FDs | 0 |
| Plaintext rows | 0 |
| Encrypted rows | 104 |
| Service restart loops | 0 |

Soak is activity-sufficient: hundreds of readiness account checks against encrypted DB material with zero session_errors.
