# Meta OAuth Phase 2 — official contract notes

Recorded: 2026-07-24

## Sources (official Meta)

- https://developers.facebook.com/docs/graph-api/reference/ (Graph API v25.0)
- https://developers.facebook.com/docs/facebook-login/guides/advanced/manual-flow
- https://developers.facebook.com/docs/facebook-login/guides/access-tokens/get-long-lived/
- https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-facebook-login/get-started
- https://developers.facebook.com/docs/instagram-platform/instagram-api-with-facebook-login/business-login-for-instagram/
- https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/page/

## Verified contract

```text
CURRENT_GRAPH_API_VERSION=v25.0
CURRENT_REQUIRED_PERMISSIONS=pages_show_list,pages_read_engagement,instagram_basic
CURRENT_LONG_LIVED_TOKEN_FLOW=fb_exchange_token via /oauth/access_token
CURRENT_PAGE_DISCOVERY_ENDPOINTS=GET /me/accounts
CURRENT_INSTAGRAM_ACCOUNT_DISCOVERY_ENDPOINTS=Page field instagram_business_account; GET /{page-id}?fields=instagram_business_account
META_REDIRECT_URI=https://ex.zellotex.com/social-agent/accounts/meta/callback
```

Publishing permissions intentionally excluded from this phase.

## Operator links

- Meta for Developers: https://developers.facebook.com/
- App Dashboard: https://developers.facebook.com/apps/
- Meta Business Suite: https://business.facebook.com/
- Graph API Explorer: https://developers.facebook.com/tools/explorer/
- App Review: https://developers.facebook.com/docs/app-review/
- Data deletion: https://developers.facebook.com/docs/development/create-an-app/app-dashboard/data-deletion-callback
