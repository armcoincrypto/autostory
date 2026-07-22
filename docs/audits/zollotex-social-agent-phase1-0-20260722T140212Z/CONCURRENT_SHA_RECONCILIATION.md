# Concurrent SHA Reconciliation (Phase 1.0 start)

## Split found
- web/readiness: `a28b54a` (story usable-session readiness) @ `20260722T140147Z-a28b54ab2c2f`
- scheduler: `181c473` (Phase 0.9) @ `20260722T123044Z-181c4731d404`

## a28b54a assessment
- Fast-forward from `181c473` (+2 commits)
- Valid: accept resolvable string sessions for Story readiness (aligned with encryption-era DB-only sessions)
- Preserves Phase 0.8/0.9 encryption readiness
- Mutation/gateway/AI defaults unchanged

## Action
Promote scheduler to existing immutable release `20260722T140147Z-a28b54ab2c2f` while keeping `transition` mode.
