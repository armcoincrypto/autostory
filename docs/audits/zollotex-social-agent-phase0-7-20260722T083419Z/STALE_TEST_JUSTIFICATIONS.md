# Stale Test Justifications

## Admin API probe `/api/accounts/session-audit`
- OLD: expect 403 on removed route
- NEW: probe `/api/v1/targets`; deny status **401**
- WHY_OLD_INVALID: route absent; scheduler auth uses `unauthorized`/401
- EVIDENCE: `scheduler_routes.py` before_request; url map
- SAFETY: deny-without-token remains strict

## Insecure admin bypass
- OLD: allow without token when `allow_insecure`
- NEW: fail-closed (401) — insecure bypass intentionally absent
- EVIDENCE: `auth_access.dashboard_api_authorized` has no bypass path
- SAFETY: strengthened

## Schema v8 / readiness UNKNOWN
- OLD: expect LEGACY_SCHEMA / ERR_LEGACY for schema 8
- NEW: compatible schema 8 → resolver None; stale v1 snapshot → UNKNOWN awaiting live proof
- EVIDENCE: `TELETHON_COMPATIBLE_SCHEMA_VERSION=8`; `test_readiness_proof_chain`
- SAFETY: unchanged (not READY)

## AI operator label "Understanding Idea"
- OLD: string present in JS
- NEW: human label map uses `Planning`
- EVIDENCE: `ai_coding_operator_mode.js`
- SAFETY: none

## RateLimiter `for_healthcheck`
- OLD: kwarg never shipped in current tree
- NEW: assert current `wait()` public API timing behavior
- SAFETY: none

## Gateway fake_send arity
- OLD: positional-only mock
- NEW: accept kwargs used by transport
- EVIDENCE: release log still shows account released in finally
- SAFETY: finally-release assertion retained

## Dexpert Kathleen audit markers
- OLD: require full Kathleen audit HTML
- NEW: accept fallback when Kathleen package blocked
- EVIDENCE: `BLOCKED_OWNER_SOURCE_REQUIRED`
- SAFETY: route remains login-gated / read-only

## Deep-health degraded without recovery
- OLD: missing recovery → degraded by accident
- NEW: force dependency failure to assert redaction
- EVIDENCE: recovery shim restores import; redaction path still required
- SAFETY: error strings still redacted
