# Architecture Decision Record

## ADR-001: Evolve AutoStory as a modular monolith

Status: proposed, blocked from production implementation by lineage certification.

Decision:

- use `/opt/autostory` / `armcoincrypto/autostory` as the canonical product lineage;
- preserve Python, Flask, Jinja, SQLAlchemy, and the existing Telegram domain where suitable;
- do not create a competing social platform or second scheduler;
- add a canonical application-service layer and typed tool registry;
- make UI routes, AI commands, scheduler executions, and future MCP adapters call those services;
- migrate production persistence to a dedicated PostgreSQL database/schema before broad multi-user/multi-provider rollout;
- use Redis only through a documented queue/locking role, not as an implicit second source of truth.

## Service ownership rule

```text
UI / JSON API / AI Assistant / Scheduler / MCP
                         |
                         v
Typed Tool Registry + Authorization + Confirmation
                         |
                         v
Canonical Application Services
                         |
                         v
Repositories + Provider Adapters + Exswaping Adapter
```

No client owns publishing, scheduling, state transitions, idempotency, or provider logic.

## Initial service contracts

- `CreateContent`
- `ReviseContent`
- `GeneratePlatformVariants`
- `ValidateContentAgainstBrand`
- `ConnectSocialAccount`
- `RefreshSocialCredential`
- `PublishContent`
- `ScheduleContent`
- `CancelSchedule`
- `GenerateImage`
- `FetchExchangeRates`
- `FetchExchangeDirections`
- `SyncAnalytics`
- `CreateCommentReplyDraft`

Each command accepts an actor/workspace context and emits an audit event. External mutations require a confirmation record and stable idempotency key.

## Tool registry contract

Each tool definition contains:

- canonical name and version;
- description;
- Pydantic input/output schemas;
- permission;
- side-effect class;
- confirmation policy;
- dry-run support;
- idempotency policy;
- timeout/retry policy;
- provider dependencies;
- audit field allowlist and secret-redaction policy.

Side-effect classes: `READ_ONLY`, `DRAFT`, `SCHEDULED_MUTATION`, `EXTERNAL_MUTATION`, `DESTRUCTIVE`.

## Reuse decisions

- reuse Telegram account/session/readiness abstractions behind a provider adapter;
- reuse scheduled-job, delivery, send-intent, and reconciliation concepts;
- consolidate the direct scheduler send and Telegram gateway send into one canonical publishing service before adding providers;
- reuse Flask-Login only as a transition mechanism; add workspace RBAC outside provider account-governance roles;
- reuse OpenAI configuration/client patterns, not the current negotiation-specific domain as the new assistant;
- reuse media storage only after adding validated metadata and a storage abstraction.

## Rejected options

- new unrelated repository/application behind the occupied hostname: duplicates working Telegram capabilities and creates routing ambiguity;
- microservices per feature/provider: no demonstrated need;
- direct Swaperex database reads: violates ownership and data minimization;
- custom OAuth protocol implementation: use official provider libraries;
- MCP-first architecture: tool registry first, thin MCP adapter later;
- SQLite as broad production persistence: current integrity history and concurrent workers make it unsuitable for the target scope.
