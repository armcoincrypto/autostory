# Open Blockers

1. Runtime owner: promote scheduler/readiness to one reviewed immutable release with rollback;
   current processes still load dirty `/opt/autostory`.
2. Application owners: classify/fix remaining functional regression failures; collection is clean
   but full suite is not green.
3. Dashboard owner: restore a source-backed protected read-only token validation endpoint and
   prove AI auto-loop stays inactive before rotating `DASHBOARD_ADMIN_TOKEN`.
4. Provider owners: rotate OpenAI/paid keys, Telegram bot token, and Telegram application
   credentials; review usage and revoke old values.
5. Security/operations: schedule `DASHBOARD_SECRET_KEY` rotation and operator re-login.
6. Source owner: recover Kathleen source; bytecode wrappers are rejected.
7. Repository administrator: approve targeted history rewrite after active rotations.
8. Swaperex owner: harden ingest/CORS/diagnostics/direct-bind endpoint boundaries.
9. Infrastructure owner: repair origin TLS.

Database FK repair, Telegram session encryption, Exswaping public-content API, and Social Agent
deployment remain separate gated phases.
