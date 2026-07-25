# Secret Installation

- Secret path: /etc/autostory/social-agent-meta.env
- Owner/mode: root / 0600
- Directory: /etc/autostory mode 0700
- Loaded by: autostory-web only
- Not loaded by: scheduler, readiness
- META_APP_ID configured: false
- META_APP_SECRET configured: false
- META_REDIRECT_URI configured: true
- SOCIAL_CREDENTIAL key configured: true (active id prod-v1)
- Key backup: /etc/autostory/backups/social-credential-prod-v1.keyenv mode 0600 (hash match verified)
- Operator note: /etc/autostory/INSTALL_META_APP_CREDENTIALS.txt
- Values exposed: false
- Telegram keys reused: false
