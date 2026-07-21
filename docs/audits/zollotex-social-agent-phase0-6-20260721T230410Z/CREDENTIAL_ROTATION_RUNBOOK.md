# Credential Rotation Runbook

For each credential: preserve metadata/fingerprint evidence; identify issuer and every consumer;
obtain a provider-generated replacement; install in a mode-`0600` non-Git location; validate
configuration without values; restart only affected services; test new acceptance; revoke old;
test old rejection; scan active config/logs/frontend; record rollback limits.

Do not rotate Telegram application credentials, bot tokens, paid-provider keys, or OAuth secrets
without their external owner. Do not rotate `DASHBOARD_SECRET_KEY` without an operator re-login
window. Do not restart AutoStory web for `DASHBOARD_ADMIN_TOKEN` while its live protected
read-only endpoints return 404 and the service configuration may activate the AI loop.

Rollback must never silently restore a compromised secret. Provider revocation may make rollback
impossible; preserve configuration shape and service binaries, not old credential validity.
