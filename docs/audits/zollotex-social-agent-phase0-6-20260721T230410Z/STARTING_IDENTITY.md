# Phase 0.6 Starting Identity

- Audit start: `2026-07-21T23:04:10Z`
- Production repository: `/opt/autostory`
- Observed production branch: `claude/deploy-bot-vps-WbBG6`
- Observed production HEAD: `68585912b9d0bdf0f2d4c393cb54ccb060a0db0a`
- Configured production upstream SHA: `45890237a6f86fd47eeb08fb28a0fb1bfb088534`
- Production worktree: dirty with tracked edits and approximately 1,400 untracked paths
- Previous authoritative observation: `6f78eb9`; superseded by current evidence
- Phase 0.5 remediation branch/SHA: `feature/zollotex-social-agent-phase0-5-remediation-20260722` / `0259f9a016fc2837cdfa1d6ec2268fc0470a808a`
- Phase 0.6 candidate branch: `fix/autostory-canonical-runtime-lineage-20260722`
- Phase 0.6 candidate base: `0259f9a016fc2837cdfa1d6ec2268fc0470a808a`
- Candidate worktree: `/opt/autostory-canonical-phase0-6`
- Existing clean production-commit worktree: `/tmp/autostory-story-safety-6858591` (preserved, detached)

## Concurrent state changes observed

During this phase another operator advanced the production branch through `491ba85` to
`499758e3465af1908fe31e197b0bd6e0e136b6ef` and restarted only the web service from
`/opt/autostory-releases/20260721T232351Z-499758e3465a`. The release manifest identifies
a `git archive` of `499758e`; data, environment, and virtualenv remain external references.
These actions were not performed by the Phase 0.6 operator.

## Baseline test collection

- Phase 0.5 base: 18 collection errors (reproduced)
- Clean current production commit `6858591`: 390 tests collected with 11 collection errors
- `6858591` adds 18 reviewed-looking Story runtime safety files relative to `6f78eb9`, but it does not restore full lineage.

No production process, database row, scheduler state, credential, or route was changed during starting verification.
