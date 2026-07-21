# Exswaping Integration Map

## Repository finding

`/root/Swaperex` is the only directly evidenced repository matching the historical Exswaping/Swaperex name. Its current README defines the product as the Kobbex non-custodial DEX at `dex.kobbex.com` and marks older Telegram/Python custodial descriptions obsolete.

Current reusable API evidence is limited to:

- public and detailed service health;
- public Fastify proxies for 1inch quotes and CoinGecko prices;
- public read-only RPC, explorer, and market-signal endpoints with provider-specific semantics;
- isolated, token-protected read-only operator telemetry for DEX monitoring, swaps, failures, revenue telemetry, wallet reconnects, and operator intelligence.

The quote/price endpoints are useful candidates for a future adapter, but they mostly pass through upstream schemas and do not provide an approved Exswaping rate definition, freshness contract, supported-direction contract, locale, or business-data classification. Operator telemetry is not an approved public marketing source.

## Requested tools and current evidence

| Tool | Safe API found | Classification | Decision |
|---|---|---|---|
| current exchange rates | 1inch quote and CoinGecko price proxies | candidate upstream data, not approved Exswaping rates | define/approve normalized contract before agent use |
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

## Existing candidate endpoints

- `GET /oneinch/swap/v6.0/{chainId}/quote`
- `GET /coingecko/simple/price`
- `GET /coingecko/markets`
- `GET /api/v1/signals`
- allowlisted read-only `/rpc/:chain` and explorer proxy routes

Only the 1inch `quote` resource should be considered for a general read-only rate adapter. The same proxy also supports unsigned transaction-building resources; those are outside the Social AI Agent boundary. Existing endpoints need schema normalization, bounded inputs, source/freshness metadata, cache policy, and explicit product-owner approval before becoming canonical tools.

The token-protected operator-intelligence route is not strictly read-only when `persistDaily=true`; its write behavior must not be exposed as a read tool and should be moved to an explicitly mutating operation in its owning service.

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
- no presentation of CoinGecko market prices or 1inch executable quotes as official Exswaping rates without an approved semantic contract;
- no mutation of Kobbex/Swaperex services as part of Social AI dry runs.
