# Production Mutation Log

| UTC | ACTION | BEFORE | AFTER | NOTES |
|---|---|---|---|---|
| 20260722T1402xxZ | concurrent a28 promote (external) | web/readiness→a28; scheduler 181 | split | story usable-session release |
| 20260722T140435Z | **Story published** (external/operator) | stories=7 | stories=8 | account_id=140; BEFORE encrypted-only; `controlled_story_publish_ok` |
| 20260722T1403xxZ | unify scheduler to a28 | split | unified a28 | transition mode |
| 20260722T141228Z | pre-encrypted-only DB backup | — | backup created | |
| 20260722T1412xxZ | mode → encrypted-only | transition | encrypted-only | config-only; same SHA a28b54a |

## Phase 1.0 observation baseline (post-activation)
Use `stories=8`, `message_deliveries=146` as T0 baseline. The +1 story occurred **before** encrypted-only activation during concurrent a28 window — recorded as safety finding, not as Phase 1.0 encrypted-only mutation.
