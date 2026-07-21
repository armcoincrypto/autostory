# Service Validation

- `swaperex-admin.service`: active on `127.0.0.1:8001`; replacement token accepted; old/missing
  rejected; no credential journal match; restart count zero after scoped restart.
- AutoStory web: active on `127.0.0.1:8000` from externally promoted immutable release `499758e`;
  this phase did not restart it.
- AutoStory scheduler: active from dirty `/opt/autostory`; mutation lock retained; not restarted.
- Readiness worker: active from dirty `/opt/autostory`; not restarted.
- Telegram gateway, bot, Kathleen listener: inactive.
- No social publishing, messaging, scheduler activation change, database repair, or Telegram
  session migration occurred.

The concurrent web deployment is recorded but not certified by Phase 0.6. Split runtime lineage
remains because scheduler/readiness do not use the immutable release.
