# Evidence bundle

Production-safe, read-only Telegram Story fleet certification. See `fleet-readiness.md` for the operator matrix and `fleet-readiness.json` for stable machine-readable data.

## Result

- Configured accounts audited exactly once: **104**
- Story-capable now: **98**
- Durable controlled-publish certified: **1** (account 140)
- Ready for operator-selected next canary: **97**
- Need authentication: **3** (accounts 13, 139, 207)
- Disabled and not connected: **3** (accounts 34, 36, 101)
- Stories published / messages sent: **0 / 0**

Account 137 is now freshly `AUTH_OK`; its prior authentication-failure
expectation is no longer current. Account 140 is `CERTIFIED_PUBLISH` from
current auth + identity + Story capability and durable Story/SystemLog
evidence, not from a hardcoded account rule.

## Deferred architectural risks (not blockers for this audit)

These were confirmed by post-audit source mapping and do **not** invalidate
the fleet classification results:

1. `Account.session_path` exists via DB migration but is not declared on the
   current ORM model; this audit resolved sessions through
   `_resolve_session_path` / canonical `account_<id>.session` / StringSession.
2. Background readiness worker policy can exclude `purpose=autostory`
   accounts; fleet certification therefore used a dedicated Story probe path
   (`CanSendStoryRequest`) rather than relying on readiness snapshots alone.
3. Generic Story rotation tick remains weaker than the controlled-live HTTP
   gate. This audit did not enable `STORY_EXECUTION_ENABLED` and did not
   exercise that path.
4. Telethon `connect` / authorization helpers may refresh disposable-copy
   session metadata. Source production session hashes were compared before
   and after each probe; mismatches: **0**.
