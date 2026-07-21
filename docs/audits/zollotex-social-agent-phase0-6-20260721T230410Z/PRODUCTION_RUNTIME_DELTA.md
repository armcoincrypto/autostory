# Production Runtime Delta

Candidate versus active immutable web release `499758e`: 229 files (177 added, 49 modified,
3 deleted). The large delta includes Phase 0/0.5 audit and remediation source, accepted dirty
runtime modules, tests, and removal of three tracked backups.

| Change class | Runtime impact | Restart/migration | Rollback |
|---|---|---|---|
| Lineage-only/docs/tests/scanners | none | none | revert commits |
| Missing-module restoration | imports become reproducible | service restart on later promotion | immutable prior release |
| Security remediation | minimal public health, restricted diagnostics, secret controls | web restart when promoted | prior release; do not restore compromised secrets |
| Functional dashboard/scheduler source | material; 56 regressions remain | web/scheduler restart | prior release + config snapshot |
| Scheduler behavior | code delta present; defaults remain locked | separate gated scheduler promotion | stop candidate/start prior release |
| Database behavior | encryption type and models included | Telegram/FK migrations explicitly not run | DB copy/transactional rollback plan |
| Telegram behavior | source changes only; gateway/bot/listener remain inactive | separate gated phase | prior release |
| AI/Social behavior | source tracked, auto-loop/publishing disabled | no activation permitted | prior release |

No candidate deployment is authorized. Promotion must first resolve regressions, verify exact
configuration, prove no automation activation, and move web/scheduler/readiness together or by a
documented staged rollout.
