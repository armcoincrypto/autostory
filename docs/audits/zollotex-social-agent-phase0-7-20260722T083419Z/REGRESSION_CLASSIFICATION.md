# Regression Classification

Baseline: 56 failures on `24e77d5` (certification checkout). Category totals equal 56.

## Category totals

| Category | Count | Resolution |
|---|---:|---|
| REAL_PRODUCT_REGRESSION | 14 | Restored `@login_required`, Account `__repr__`, session-lock `add_account`/`connect_with_reason`, profile capability persistence |
| STALE_TEST_EXPECTATION | 25 | Retargeted auth probes (401, `/api/v1/targets`), auth patch symbols, schema/UNKNOWN contracts, gateway kwargs, rate-limiter API, AI labels, health helpers restored or expectations aligned |
| DATABASE_STATE_DEPENDENCY | 11 | `tests/helpers/fleet_seed.py` seeds 107/110/139/140 + mentions/targets |
| OBSOLETE_RECOVERY_MODULE | 4 | Minimal reviewed `src/recovery/` compatibility shims (no Telegram mutation) |
| UNSUPPORTED_KATHLEEN_PATH | 1 | Dexpert audit accepts Kathleen-bridge fallback; listener remains stopped |
| CONFIGURATION_MISMATCH | 1 | Cookie Secure test forces `ENVIRONMENT=production` |
| **TOTAL** | **56** | |

## Module map (abbrev)

- Admin/login: REAL×7, STALE×7, UNSUPPORTED_KATHLEEN×1, CONFIG×1
- Story/readiness/ops: STALE×14, DATABASE×11, OBSOLETE_RECOVERY×4
- Client/misc: REAL×7, STALE×4

Post-fix suite: `670 passed, 13 skipped, 0 failed` (683 collected). Original 56 nodeids passed twice (flake check). One adjacent test updated after recovery shim made deep-health healthy by default (`test_restricted_diagnostics_redacts_dependency_errors` now forces failure to assert redaction).
