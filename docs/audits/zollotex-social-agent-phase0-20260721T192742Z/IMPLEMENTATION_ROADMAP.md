# Implementation Roadmap

## Gate 0A — lineage and runtime certification

Before Phase 1 code:

1. freeze feature mutation in the production worktree;
2. inventory and attribute every tracked/untracked production change;
3. separate runtime artifacts and secrets from source;
4. push/review the 40 local commits or select another explicit baseline;
5. capture necessary dirty source changes in coherent reviewed commits without overwriting other agents;
6. prove runtime tree equality to one commit;
7. correct origin TLS through the approved operator process;
8. classify and repair the 910 FK violations on a copy, then rehearse migration;
9. restrict secret artifacts, remove tracked backups through a reviewed incident response, and rotate affected credentials;
10. design and rehearse encryption migration for Telegram session material;
11. document current scheduler activation and reconcile stale audit claims.

Gate: `ZOLLOTEX_SOCIAL_AGENT_PHASE_0_DISCOVERY_PASS`

## Phase 1 — foundation

- modular package boundaries for identity, workspaces, tools, content, social, publishing, media, audit, health, and Exswaping;
- PostgreSQL schema and Alembic;
- operator login migration and workspace RBAC;
- typed service result/error contracts;
- typed tool registry with permissions, confirmation, idempotency, and dry-run metadata;
- unified audit service;
- admin-only diagnostics with safe metadata;
- initial Dashboard, AI Assistant shell, Social Accounts, and Settings;
- immutable release build, manifest, lock, smoke, and rollback scripts;
- isolated loopback preview with no public nginx route.

Gate: `ZOLLOTEX_SOCIAL_AGENT_PHASE_1_FOUNDATION_PASS`

## Phase 2 — assistant and read-only tools

- conversations/messages/streaming;
- structured tool requests;
- confirmation cards;
- dry-run mode;
- AI usage/cost records;
- draft content service;
- Exswaping tools only after approved APIs exist;
- source and freshness display.

Gate: `ZOLLOTEX_SOCIAL_AGENT_PHASE_2_AI_CHAT_PASS`

## Phase 3 — Telegram adapter

- canonical adapter over reused Telegram implementation;
- connection metadata and health;
- destination resolution;
- dry-run payloads;
- canonical publish service;
- independent destination results;
- uncertain-outcome reconciliation;
- no real publication.

Gates: connection and dry run only. Canary remains separately authorized.

## Later bounded phases

- content lifecycle and approvals;
- scheduling/calendar using the existing scheduler concepts;
- media library and image-generation adapter;
- provider-by-provider integrations;
- analytics;
- comments;
- messages;
- versioned brand knowledge;
- suggestion-only automations.

## First restricted release

Target only:

- secure operator login and RBAC;
- truthful dashboard;
- AI assistant with draft/read-only tools;
- approved Exswaping public facts;
- Telegram connection status and dry-run preview;
- controlled scheduling records with live execution disabled;
- media upload;
- audit history.

Automatic publishing remains disabled.
