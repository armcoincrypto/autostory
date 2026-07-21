# Security Model

## Trust boundaries

Untrusted inputs:

- operator text and uploaded files;
- AI model output;
- social posts, comments, messages, profiles, and webhooks;
- Exswaping API responses;
- imported brand documents;
- provider errors and redirects;
- browser-supplied identifiers and idempotency keys.

Trusted enforcement must remain in backend code, never in prompts or frontend controls.

## Required controls

- workspace-scoped RBAC with Owner, Administrator, Editor, Approver, Analyst, and Viewer roles;
- tool-level permission checks before model-requested calls;
- explicit, expiring confirmation records for external mutations;
- CSRF protection for cookie-authenticated mutations;
- OAuth state and PKCE where supported;
- encrypted provider credentials with key versioning and rotation;
- token values never returned after storage;
- constant-time secret comparisons;
- webhook signatures, timestamps, replay deduplication, and payload size limits;
- outbound URL allowlists and SSRF-resistant fetchers;
- file-content MIME detection, size/dimension/duration limits, SVG sanitization or rejection, decompression limits, safe filenames, checksums, and malware scanning where available;
- structured secret redaction in logs and audit payloads;
- secure cookies, CSP, frame denial, nosniff, referrer policy, HSTS after origin TLS is corrected, and rate limits;
- idempotency, uncertain-outcome reconciliation, and per-destination results;
- prompt boundary labels separating system policy, organization policy, user input, external content, and tool results.

## Current controls worth retaining

- production mutation kill switches default closed;
- centralized execution guard for existing Telegram actions;
- send-intent/idempotency fields and delivery reconciliation;
- CSRF-protected operator login;
- secure/HttpOnly/SameSite cookie evidence from prior certification;
- account allowlists, binding permission checks, and controlled canary authorization;
- secret values were not printed during this audit.

## Current gaps

- no human workspace RBAC;
- optional shared admin token is broader than required;
- API auth allows legacy authenticated users with null admin state;
- provider/session secrets are stored in host `.env` and filesystem session files without a demonstrated application-level envelope-encryption model;
- live and backup environment files are mode `0644`, and three secret-bearing backups are tracked by Git;
- `accounts.session_string` is stored in a plain text column without demonstrated universal encryption enforcement;
- some sensitive account metadata is embedded in systemd drop-ins;
- web/readiness services run as root;
- broad API CSRF exemptions make correctness depend on every route applying authentication and origin/session protections consistently;
- full phone numbers are exposed by some internal APIs/log paths;
- no typed tool authorization/confirmation framework;
- no general prompt-injection containment;
- no provider credential metadata/rotation model;
- no production release isolation;
- origin TLS is expired and SAN-incomplete;
- SQLite has 910 FK violations.

## Threat model priorities

1. Unauthorized social publishing or message sending.
2. Duplicate external posts after retries/timeouts.
3. Credential/session theft.
4. Secret persistence in Git history and backup artifacts.
5. Prompt injection causing unauthorized tool use.
6. Cross-workspace data exposure.
7. Stale or fabricated Exswaping facts presented as current.
8. Malicious upload or webhook processing.
9. Deployment from an unreviewed dirty runtime.
10. Provider capability overclaiming and false success reporting.

## Fail-closed rules

- missing permission, credential, scope, destination, freshness, confirmation, or idempotency state denies execution;
- unknown provider outcome becomes `UNKNOWN_REQUIRES_RECONCILIATION`, never automatic success or blind retry;
- partial publication records each destination independently;
- stale rates block “current rates” publication;
- retrieved text cannot grant permissions or alter tool policies;
- dry run cannot invoke provider mutations or paid media generation.
