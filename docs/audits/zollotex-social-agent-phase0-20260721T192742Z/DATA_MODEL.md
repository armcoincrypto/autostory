# Data Model

## Persistence decision

Use PostgreSQL for the target production model after a reviewed migration. Keep the current SQLite database read-compatible during transition; do not perform destructive conversion in place.

All tenant-owned tables include `workspace_id`, stable opaque identifiers, `created_at`, `updated_at`, and deliberate indexes. Provider-specific payloads may use versioned JSON, but authorization, lifecycle, destination, status, idempotency, and queryable metrics remain relational.

## Core aggregates

### Identity

- `users`
- `workspaces`
- `memberships`
- `roles`
- `permissions`
- `role_permissions`
- `user_settings`

### AI and tools

- `conversations`
- `conversation_messages`
- `ai_runs`
- `tool_definitions`
- `tool_calls`
- `tool_call_confirmations`
- `prompt_templates`
- `ai_usage_records`

### Social connections

- `social_connections`
- `provider_accounts`
- `social_destinations`
- `encrypted_credentials`
- `credential_scopes`
- `credential_refresh_events`
- `connection_health_events`

### Content and publishing

- `content_items`
- `content_revisions`
- `platform_variants`
- `content_approvals`
- `content_tags`
- `campaign_metadata`
- `publish_operations`
- `publish_destinations`
- `publish_attempts`
- `external_posts`
- `schedules`
- `recurring_schedule_rules`
- `dead_letter_records`

### Media and brand

- `media_assets`
- `media_variants`
- `media_generation_jobs`
- `brand_profiles`
- `brand_profile_versions`
- `brand_rules`
- `knowledge_sources`
- `knowledge_source_versions`
- `knowledge_chunks`
- `content_validation_reports`

### Engagement and operations

- `analytics_snapshots`
- `comments`
- `comment_replies`
- `message_threads`
- `messages`
- `sync_cursors`
- `automation_rules`
- `automation_executions`
- `audit_logs`
- `application_events`
- `integration_errors`
- `webhook_receipts`
- `deduplication_records`

## Publishing lifecycle

Allowed item states:

`DRAFT -> READY_FOR_REVIEW -> APPROVED -> SCHEDULED -> PUBLISHING -> PUBLISHED|PARTIALLY_PUBLISHED|FAILED`

Additional transitions:

- `READY_FOR_REVIEW -> CHANGES_REQUESTED -> DRAFT`
- pre-publication states may move to `CANCELLED`;
- terminal content may move to `ARCHIVED`;
- failed destinations may retry independently;
- published destinations never re-execute for the same operation key.

Each destination stores its own status, provider account, destination, request fingerprint, idempotency key, attempt count, external ID, timestamps, error category, and reconciliation state.

## Migration rules

- use Alembic revisions, not startup-time ad hoc DDL;
- review generated SQL;
- use expand-and-contract;
- add constraints only after data cleanup;
- no destructive rollback;
- retain current Telegram identifiers and delivery evidence;
- do not import the 910 legacy FK violations into a new constrained schema without classification and repair.
