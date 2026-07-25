# OAuth Flow

Live OAuth not started because META_APP_ID and META_APP_SECRET are missing.

When credentials are installed, expected flow remains the already-deployed implementation:
- Connect Meta creates single-use OAuth state (TTL 600s)
- Redirect host Facebook only
- Scopes: pages_show_list, pages_read_engagement, instagram_basic
- Callback path: /social-agent/accounts/meta/callback
- Server-side token exchange and encrypted persistence
