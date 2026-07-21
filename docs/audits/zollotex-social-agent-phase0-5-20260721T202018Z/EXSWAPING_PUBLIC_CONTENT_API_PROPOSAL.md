# Exswaping Public Content API Proposal

## Ownership

The API must be owned by the canonical Exswaping application or an explicitly approved Exswaping integration service. AutoStory/Social Agent is a read-only consumer. It must not derive business truth from 1inch, CoinGecko, RPC/explorer data, HTML, or direct database access.

Recommended repository ownership is unresolved between the current Exswaping/Swaperex runtime and a future canonical backend. No implementation is authorized until that owner is named.

## Versioned endpoints

```text
GET /api/public-content/v1/exchange-directions
GET /api/public-content/v1/exchange-rates
GET /api/public-content/v1/reserves
GET /api/public-content/v1/promotions
GET /api/public-content/v1/news
GET /api/public-content/v1/blog
GET /api/public-content/v1/referral-program
GET /api/public-content/v1/company-profile
GET /api/public-content/v1/content-health
```

Support `locale`, bounded pagination, and explicit item identifiers. Unknown locales fail or return an explicit fallback warning; they must not silently masquerade as requested localization.

## Common envelope

```json
{
  "schema_version": "1.0",
  "generated_at": "RFC3339 UTC",
  "source_updated_at": "RFC3339 UTC",
  "locale": "en",
  "freshness_seconds": 12,
  "data_classification": "PUBLIC_APPROVED",
  "items": [],
  "warnings": []
}
```

Errors use a stable code, HTTP status, request ID, retryability, and last-known-source timestamp. A stale cache is never relabeled current.

## Rate and direction semantics

Each rate item must include:

- stable direction ID;
- source/destination asset and network identifiers;
- displayed customer rate and rate type;
- minimum/maximum customer amount;
- public reserve availability state, not internal liquidity formulas;
- availability state and reason code;
- source timestamp and expiry;
- quote limitations/disclaimer.

The endpoint must distinguish indicative displayed rates from executable order quotes. It must not expose internal spread/commission formulas, counterparty balances, private reserve amounts, or customer-specific pricing.

## Access model

Recommended initial model: TLS plus a narrow service credential with audience `zollotex-social-agent` and scope `public-content:read`. Send credentials only in the `Authorization` header; never query strings. Enforce rate limits, timing-safe credential validation, no-store on authenticated diagnostics, request/audit IDs, and key rotation.

Separately approved marketing content may later be exposed publicly through a rate-limited/cacheable façade. mTLS can be added for internal deployment but should not replace application-level audience/scope checks. The Social Agent must not depend on an unauthenticated internal-network assumption.

## Proposed freshness and caching defaults

These are approval candidates, not business decisions:

| Data | Current-claim threshold | Cache | Stale behavior |
|---|---:|---|---|
| Customer displayed rates | 60 seconds | private 15 seconds; ETag | Block “current/today” claims after threshold |
| Directions/networks | 5 minutes | private 60 seconds; ETag | Allow draft with stale warning; block publish if availability claim is material |
| Reserve availability state | 60 seconds | private 15 seconds | Return unavailable/unknown; never infer |
| Promotions | 5 minutes | private 60 seconds | Exclude expired/not-yet-active items |
| News/blog | 5 minutes | private 60 seconds | May use last approved item with explicit publication timestamp |
| Referral program | 1 hour | private 5 minutes | Block numeric claims if stale |
| Company profile | 24 hours | private 1 hour | Allow approved version with version timestamp |
| Content health | 30 seconds | no-store | Report dependency state without internal topology |

Use ETags and `Last-Modified`; honor conditional GET. Partial responses must identify omitted sections and warnings. The Social Agent records endpoint, item IDs, schema version, retrieval time, source update time, and freshness with each generated factual claim.

## Integrity and change control

- Publish only records approved for public classification.
- Validate schema at producer and consumer.
- Sign release/content versions or provide tamper-evident audit records where practical.
- Version breaking changes under `/v2`; add fields compatibly in v1.
- Maintain deprecation notices and consumer telemetry without logging credentials.
- Test stale, unavailable, partial, locale, authorization, rate-limit, and rollback behavior.

## Approval boundary

```text
EXSWAPING_PUBLIC_CONTENT_API_OWNER=UNRESOLVED
EXSWAPING_REPOSITORY_PATH=UNRESOLVED
APPROVED_AUTH_MODEL=UNRESOLVED (proposal: scoped service bearer token over TLS)
APPROVED_FIELDS=UNRESOLVED
APPROVED_FRESHNESS_THRESHOLDS=UNRESOLVED
APPROVED_DEPLOYMENT_PHASE=UNRESOLVED
EXSWAPING_BUSINESS_CONTENT_TOOLS=BLOCKED
```
