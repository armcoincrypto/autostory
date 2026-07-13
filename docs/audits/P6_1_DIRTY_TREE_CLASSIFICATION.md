# P6.1 Dirty Tree Classification

Generated during P6.1 baseline inspection. Do not reset or clean without review.

- Modified tracked files: **47**
- Untracked files: **2240**
- P6 committed state at HEAD `9aa201e` is clean for operator_control paths

## Modified tracked files by category
### older unrelated implementation (31)
should_commit: only if part of intentional P6.1 scope
- `src/bot/__init__.py`
- `src/bot/bot.py`
- `src/clients/__init__.py`
- `src/clients/manager.py`
- `src/clients/session_resolve.py`
- `src/core/database.py`
- `src/core/models.py`
- `src/core/safety_policy.py`
- `src/core/session_lock.py`
- `src/core/session_paths.py`
- `src/dashboard/__init__.py`
- `src/dashboard/ai_coding_routes.py`
- `src/dashboard/routes.py`
- `src/dashboard/scheduler_routes.py`
- `src/dashboard/static/js/ai_coding_operator_home.js`
- `src/dashboard/static/js/ai_coding_operator_mode.js`
- `src/dashboard/templates/accounts.html`
- `src/dashboard/templates/ai_coding.html`
- `src/dashboard/templates/index.html`
- `src/dashboard/templates/login.html`
- `src/dashboard/templates/scheduler.html`
- `src/dashboard/templates/scheduler_modals.html`
- `src/dashboard/templates/stories.html`
- `src/discovery/scanner.py`
- `src/publisher/story_publisher.py`
- ... and 6 more

### P6/P6.1 source (6)
should_commit: only if part of intentional P6.1 scope
- `src/dashboard/operator_control_routes.py`
- `src/dashboard/operator_control_service.py`
- `src/dashboard/templates/operator/bindings.html`
- `src/dashboard/templates/operator/deliveries.html`
- `src/dashboard/templates/operator/schedules.html`
- `src/dashboard/templates/operator/system_safety.html`

### tests (6)
should_commit: only if part of intentional P6.1 scope
- `tests/conftest.py`
- `tests/test_ai_coding_operator_home_ux.py`
- `tests/test_dashboard_login.py`
- `tests/test_scheduler_executor_idempotency.py`
- `tests/test_scheduler_send_intent.py`
- `tests/test_telegram_gateway.py`

### unknown ownership (4)
should_commit: only if part of intentional P6.1 scope
- `.env.example`
- `.gitignore`
- `deploy/autostory-web.service`
- `requirements.txt`

## Untracked files by category
### unknown ownership (1671)
recommended: review individually; do not bulk commit
- `.env.backup.1778420422`
- `.env.backup.1778437253`
- `.env.backup.1778437410`
- `.env.backup.1778438111`
- `.env.p0_backup_20260709T001207Z`
- `.env.p1_backup_20260709T002735Z`
- `.env.p3_activation_backup_20260709T121518Z`
- `.env.p4a_backup_20260709T122242Z`
- ... and 1663 more

### older unrelated implementation (301)
recommended: review individually; do not bulk commit
- `scripts/audit/broken_scheduler_jobs.py`
- `scripts/audit/p92_archive_pyc.sh`
- `scripts/audit/p92_inventory.sh`
- `scripts/audit/p92_pyc_match.py`
- `scripts/audit/p92_stash_extract.sh`
- `scripts/audit/p92b_bruteforce_cursor_dir.py`
- `scripts/audit/p92b_bruteforce_git_candidates.py`
- `scripts/audit/p92b_decompile_attempt.sh`
- ... and 293 more

### tests (150)
recommended: review individually; do not bulk commit
- `tests/test_account_operational_state.py`
- `tests/test_accounts_legacy_redirect.py`
- `tests/test_accounts_v2_fleet_listing.py`
- `tests/test_accounts_v2_preview.py`
- `tests/test_accounts_v2_routes.py`
- `tests/test_ai_agent_account_allowlist.py`
- `tests/test_ai_agent_autonomous_guards.py`
- `tests/test_ai_agent_conversation_state.py`
- ... and 142 more

### audit evidence (112)
recommended: commit only certification artifacts when scoped
- `data/audit/campaign_governance.jsonl`
- `data/audit/operator_login_csrf_recovery_20260711T140500Z.json`
- `data/audit/p3_risk_monitor_20260709T120535Z.json`
- `data/audit/p3_risk_monitor_20260709T121658Z.json`
- `data/audit/p3_risk_monitor_20260709T144314Z.json`
- `data/audit/p3_risk_monitor_20260709T152829Z.json`
- `data/audit/p3_risk_monitor_20260709T160001Z.json`
- `data/audit/p3_risk_monitor_20260709T220001Z.json`
- ... and 104 more

### generated cache (6)
recommended: ignore
- `.cursor/aicoding/rules/01-product-vision.mdc`
- `.cursor/aicoding/rules/02-human-intervention-minimization.mdc`
- `.cursor/aicoding/rules/03-operator-experience.mdc`
- `.cursor/aicoding/rules/04-production-safety.mdc`
- `.cursor/aicoding/rules/05-autonomous-completion.mdc`
- `.cursor/permissions.json`

## Critical constraints
- P6/P6.1 operator_control files are committed and isolated
- ~4000 untracked paths are mostly backups and recovery_lab artifacts
- Do not use git clean -fd or git reset --hard
