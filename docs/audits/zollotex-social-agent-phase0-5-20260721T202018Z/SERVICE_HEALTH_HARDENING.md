# Service Health Hardening

## Three-level contract

### Public liveness

`GET /api/health` returns only:

```json
{"status":"healthy"}
```

No service name, version, uptime, process, path, database, queue, provider, account, wallet, revenue, or release topology is included.

### Authenticated readiness

Readiness should report only coarse dependency states (`ready|degraded|unavailable`) to an authorized operator session or scoped service monitor. It must use `Cache-Control: no-store`, bounded execution time, request IDs, and no raw exception text.

### Restricted diagnostics

Detailed queue/lock/provider diagnostics require explicit administrator authorization, header-only token compatibility during transition, timing-safe token comparison, rate limiting, audit logging, and `no-store`. Query-string tokens are rejected.

## AutoStory patch prepared

- `/api/health` minimized.
- `/api/health/deep` changed from public to explicit operator authorization.
- Missing/incorrect token fails closed.
- Query-string tokens are not accepted by the restricted operator helper.
- Unauthorized, rate-limited, and successful diagnostic responses use `no-store`.
- In-process diagnostic limit: 30 requests/minute/client as defense in depth.
- No permissive CORS header is added.
- Focused tests cover the boundary.

The patch is not deployed. The Phase branch cannot fully boot from committed source because required AI modules exist only as untracked production files; promotion remains blocked.

## Swaperex recommendations

- Cache provider-backed Fastify health for 15–30 seconds and run provider checks concurrently with abortable deadlines.
- Preserve a minimal process-health endpoint separately from provider readiness.
- Keep FastAPI detailed health loopback/private.
- Do not repurpose `/api/v1/admin/health`; the admin login probe depends on its simple contract.
- Add `no-store` to operator health/telemetry and route-specific rate limits.
- Remove public `/rpc/test` and `/explorer/test`.

No Swaperex endpoint was changed because repository ownership, branch lineage, compatibility, and release authority were not established for this AutoStory remediation branch.
