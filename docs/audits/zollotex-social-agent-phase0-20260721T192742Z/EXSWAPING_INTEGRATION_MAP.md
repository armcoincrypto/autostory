# Exswaping Integration Map

## Repository finding

`/root/Swaperex` is the only directly evidenced repository matching the historical Exswaping/Swaperex name. Its current README defines the product as the Kobbex non-custodial DEX at `dex.kobbex.com` and marks older Telegram/Python custodial descriptions obsolete.

Current reusable API evidence is limited to:

- public and detailed service health;
- isolated, token-protected read-only operator telemetry for DEX monitoring, swaps, failures, revenue telemetry, wallet reconnects, and operator intelligence.

These are operational DEX metrics, not the approved public marketing facts required by the Social AI Agent.

## Requested tools and current evidence

| Tool | Safe API found | Classification | Decision |
|---|---|---|---|
| current exchange rates | no | missing | block implementation |
| supported exchange directions | no | missing | block implementation |
| network information | frontend token/network configuration exists, but no approved public business API | unapproved source | define API contract |
| public reserve availability | no | missing/sensitive | do not infer |
| active public promotions | no | missing | define content API |
| recent public news | no | missing | define content API |
| published blog articles | no | missing | define content API |
| referral-program information | no | missing | define content API |
| approved company information | no | missing | define versioned public facts API |

## Required adapter response

Every Exswaping adapter response must include:

- `source`;
- `retrieved_at`;
- `source_updated_at` when available;
- `freshness_seconds`;
- `locale`;
- `classification=PUBLIC`;
- `status`;
- typed data;
- typed error when unavailable.

Rates require a configured maximum age. Stale or unavailable rates must never be described or published as current.

## Proposed safe API boundary

An Exswaping-owned service should publish authenticated read-only endpoints, for example:

- `GET /public-content/v1/rates`
- `GET /public-content/v1/directions`
- `GET /public-content/v1/networks`
- `GET /public-content/v1/reserves`
- `GET /public-content/v1/promotions`
- `GET /public-content/v1/news`
- `GET /public-content/v1/articles`
- `GET /public-content/v1/referral`
- `GET /public-content/v1/company`

This proposal is not implemented. The owning Exswaping service must define semantics, freshness, locale, and public-data classification.

## Prohibited integration

- no direct production database access;
- no use of private orders, KYC, payments, balances, withdrawal controls, key material, seed phrases, xpubs, or admin-only reserve controls;
- no reuse of DEX telemetry as customer-facing rates;
- no mutation of Kobbex/Swaperex services as part of Social AI dry runs.
