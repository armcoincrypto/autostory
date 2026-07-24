# Meta operator setup runbook (ex.zellotex.com)

## Secrets (never commit)

Install outside Git / outside immutable release, mode `0600`:

```text
META_APP_ID
META_APP_SECRET
META_REDIRECT_URI=https://ex.zellotex.com/social-agent/accounts/meta/callback
META_APP_MODE=development|live
META_FACEBOOK_PUBLISHING_ENABLED=false
META_INSTAGRAM_PUBLISHING_ENABLED=false
SOCIAL_CREDENTIAL_ACTIVE_KEY_ID=sa-cred-1
SOCIAL_CREDENTIAL_KEYS_JSON={"sa-cred-1":"<base64url-32-byte-key>"}
```

Generate a key:

```bash
python3 -c 'import os,base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())'
```

Add Valid OAuth Redirect URI in Meta App Dashboard exactly matching `META_REDIRECT_URI`.

## Flow

1. Deploy code with Meta unconfigured (or configured + encryption keys).
2. Open `/social-agent/accounts`.
3. Connect Meta → authorize with Page admin.
4. Select Facebook Page / Instagram Professional account.
5. Run health check.
6. Do **not** enable publishing flags.

## Rollback

Previous release retains encrypted rows. Old code may hide Meta UI; tokens remain encrypted at rest until disconnect on a build that supports it.
