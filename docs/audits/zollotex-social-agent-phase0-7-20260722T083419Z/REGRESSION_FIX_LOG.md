# Regression Fix Log

1. Restored `@login_required` on `/`, `/accounts`, `/stories`, `/discovery`, `/campaigns`, `/scheduler`.
2. Restored health `story_from_db` helpers on dashboard routes.
3. Aliased `routes._admin_api_allowed` for source loads.
4. Fixed `Account.__repr__` when status unset at construction.
5. Restored `connect_with_reason`, lock-aware `add_account`, profile capability persistence.
6. Added minimal `src/recovery/` compatibility shims (constants + read-only stubs).
7. Added fleet seed helper + dry-run `scripts/ops/p10_19_story_authorization_probe.py`.
8. Updated stale auth/login/readiness/gateway/rate-limiter/AI-label tests with evidence.
9. Forced deep-health dependency failure in redaction test after recovery shim.
