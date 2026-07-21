# Open Blockers

## Critical

1. **AutoStory lineage is not certifiable.** Local HEAD is 40 commits ahead of upstream and production includes large uncommitted source changes.
2. **Production is not immutable.** systemd imports code directly from the dirty Git worktree.
3. **Origin TLS is invalid.** The configured certificate is expired and omits `ex.zellotex.com`, even though Cloudflare edge TLS is valid.
4. **Database referential integrity fails.** SQLite quick check passes but 910 foreign-key violations remain.
5. **No approved Exswaping public-content API exists.** Required rates/directions/reserves/news tools cannot be implemented honestly from current evidence.

## High

6. Current authentication lacks workspace RBAC and least-privilege permissions.
7. Existing JSON API auth includes legacy null-admin compatibility and a broad shared-token option.
8. Telegram publishing has two execution rails that must be consolidated before expansion.
9. Some effective systemd configuration drifts from repository units and includes account metadata.
10. Several application processes run as root.
11. Secrets/session encryption, credential rotation, and redaction controls do not yet satisfy multi-provider OAuth requirements.
12. Current AI Agent is a Telegram negotiation assistant, not the required general structured-tool assistant.
13. No typed canonical tool registry exists.

## External/operator actions

- designate an owner to reconcile and push/review AutoStory runtime lineage;
- approve repair/renewal of origin TLS;
- approve a clean baseline SHA after runtime reconciliation;
- assign Exswaping API ownership and provide an approved read-only contract;
- decide whether the existing hostname remains the final in-place product route or provide a distinct preview hostname;
- provide identity-provider direction and initial operator role assignments;
- later provide provider app credentials/test accounts through approved secret storage.

## Explicitly not blockers for isolated design work

- credentials for Meta/X/LinkedIn/Discord/YouTube/TikTok;
- real Telegram canary authorization;
- paid image/video provider selection.

Those are later phase gates. Phase 1 production implementation remains blocked by the critical items above.
