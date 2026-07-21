# Open Blockers

## Critical

1. **AutoStory lineage is not certifiable.** Local HEAD is 40 commits ahead of upstream and production includes large uncommitted source changes.
2. **Production is not immutable.** systemd imports code directly from the dirty Git worktree.
3. **Origin TLS is invalid.** The configured certificate is expired and omits `ex.zellotex.com`, even though Cloudflare edge TLS is valid.
4. **Database referential integrity fails.** SQLite quick check passes but 910 foreign-key violations remain.
5. **Secret artifacts are exposed.** The live environment file and three Git-tracked environment backups are mode `0644`; more untracked backups exist.
6. **Telegram session encryption is unproven.** Session strings use a plain text database column without demonstrated universal encryption enforcement.
7. **No approved Exswaping public-content API exists.** Public quote/price passthroughs exist, but required official rates/directions/reserves/news semantics are not approved.

## High

8. Current authentication lacks workspace RBAC and least-privilege permissions.
9. Existing JSON API auth includes legacy null-admin compatibility and a broad shared-token option.
10. Telegram publishing has overlapping scheduler, gateway, Celery, and duplicate story-publisher implementations that must be consolidated.
11. Some effective systemd configuration drifts from repository units and includes account metadata.
12. Several application processes run as root.
13. Secrets/session encryption, credential rotation, and redaction controls do not yet satisfy multi-provider OAuth requirements.
14. Current AI Agent is a Telegram negotiation assistant, not the required general structured-tool assistant.
15. No typed canonical tool registry exists.
16. Swaperex environment naming (`ENVIRONMENT` versus `SWAPEREX_ENV`) and a mutating GET option require owner-side correction before reuse.

## External/operator actions

- designate an owner to reconcile and push/review AutoStory runtime lineage;
- approve repair/renewal of origin TLS;
- restrict secret-file permissions, quarantine backups, and perform a reviewed Git-history/credential-rotation response;
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
