# Phase 0.5 Test Report

## Passing validation

| Command / check | Result |
|---|---|
| `/opt/autostory/venv/bin/python -m pytest -q tests/test_phase05_endpoint_boundaries.py tests/test_secret_artifact_audit.py tests/test_phase05_fk_repair.py tests/test_telegram_session_encryption.py tests/test_scheduler_runtime_capture.py tests/test_public_route_inventory.py` | **25 passed in 3.31s** |
| `/opt/autostory/venv/bin/python -m compileall -q config src scripts` | passed |
| `/opt/autostory/venv/bin/python -m pip check` | `No broken requirements found` |
| `git diff --check` | passed |
| `.gitignore` checks for new environment backup patterns | passed (3/3 ignored) |
| IDE diagnostics on changed source/scripts | no linter errors |
| Secret artifact scanner after containment | 25 artifacts, 22 real secret artifacts, zero insecure real-artifact modes |
| Static route inventory | 8 source/nginx files, 96 declarations, no imports/execution |
| FK classifier | 910 returned, 910 classified |
| FK repair copy rehearsal | 910 before, 0 after, checks `ok`, aggregates stable, second run zero changes |
| Telegram encryption tests | included in focused 25; tamper/wrong/missing key and protected target fail closed |
| Telegram copy rehearsal | 104 plaintext before, 104 migrated, 0 plaintext after, second run zero changes |
| ORM encrypted-copy read | 104 rows decrypted through centralized type; no Telegram connection |
| Corrected runtime-source app smoke against repaired copy | HTTP 200, 104 accounts, 366 scheduler rows, readiness/AI loops disabled |

## Full regression result

Command:

```text
READINESS_WORKER_ENABLED=false AI_AGENT_AUTO_LOOP_ENABLED=false /opt/autostory/venv/bin/python -m pytest -q
```

Result: **blocked during collection with 18 import errors**. Missing tracked modules include `src.recovery`, `src.stories.run_batch`, `src.stories.scheduler_integration`, `src.clients.readiness_store`, `src.core.account_runtime_state`, `src.core.datetime_utc`, and `src.core.account_operational_state`.

The committed app factory separately imports untracked production-only `src/core/ai_agent_models.py` and `src/dashboard/ai_agent_routes.py`. These are pre-existing repository/runtime lineage defects. They were not papered over by copying dirty production files into the remediation branch.

## Safety notes

An early isolated app smoke omitted `READINESS_WORKER_ENABLED=false`; dirty runtime source started a readiness cycle against the copied database before process shutdown. No live DB was selected and no publish/send route was called. The corrected smoke explicitly disabled readiness and AI loops and passed.

No live HTTP/RPC/provider endpoint was called as a test. No service restart was used for validation.
