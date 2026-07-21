# Operator Telemetry Boundary

## Current boundary

Swaperex admin telemetry is served by a loopback FastAPI app on port 8001 and exposed through nginx under `/api/v1/admin/*`. It requires `X-Admin-Token`, uses constant-time comparison, fails with 503 when the server token is missing, and returns 401 for missing/wrong caller tokens.

The routes cover process health, overview, raw events, swaps, revenue, normalized/reconciled revenue, swap lifecycles, health alerts, failures, wallet reconnects, and operator intelligence.

## Gaps

- One shared token grants every route; there is no RBAC or scope separation.
- No route/auth-failure rate limit exists.
- Responses lack an explicit universal `Cache-Control: no-store`.
- Expensive aggregates can scan large datasets beyond nginx’s 15-second timeout.
- `operator-intelligence?persistDaily=true` mutates state through GET.
- A frontend build-time token fallback can place the operator token in a public bundle.
- Session-storage token handling remains vulnerable to same-origin XSS.
- Old frontend clients call a missing `/admin/lifecycle` route and misuse `/admin/health`.
- Monitoring ingest is a separate public route and is effectively unauthenticated when its optional server secret is unset.

## Required target

1. Replace static browser-embedded credentials with an HttpOnly, Secure, SameSite operator session or a separate admin origin.
2. Retain header-token support only for scoped service monitors during migration.
3. Apply explicit roles/scopes for raw events, financial aggregates, and health.
4. Add low authentication-failure limits and bounded per-route budgets.
5. Add `no-store`, request IDs, access audit records, bounded time windows, and pagination.
6. Move snapshot persistence to an explicit audited POST.
7. Remove `VITE_ADMIN_API_TOKEN` support and scan built assets.
8. Correct stale frontend contracts without changing login-health semantics.
9. Strictly schema/rate/retention-limit public monitoring ingest; do not put a static ingest secret in Vite.
10. Rotate the now-exposed admin token after consumers are mapped.

## AutoStory restricted diagnostics

The Phase branch adds a separate `operator_api_authorized()` helper for deep diagnostics. It accepts only an explicit admin session or timing-safe header token; query tokens are never accepted. This does not authorize or alter Swaperex telemetry.

## Status

AutoStory hardening is implemented and tested but undeployed. Swaperex hardening is a reviewed proposal only. Therefore the overall operator-telemetry boundary remains blocked for production certification.
