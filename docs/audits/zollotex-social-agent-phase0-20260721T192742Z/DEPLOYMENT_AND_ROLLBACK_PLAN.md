# Deployment and Rollback Plan

Status: design only; no deployment performed.

## Required release topology

- build from a clean, reviewed, remote-backed commit;
- produce an immutable release directory;
- keep configuration, logs, media, sessions, and backups in shared paths;
- write a release manifest containing SHA, build ID, migration revision, nonsecret configuration fingerprint, timestamp, and rollback target;
- use a deployment lock;
- run database expansion migrations before switching code;
- atomically update `current`;
- restart only dedicated Social Agent services;
- validate health and then reload nginx only when a routing change is separately approved.

## Pre-promotion gates

- expected repository, branch, remote, and SHA;
- clean tree;
- secret scan and dependency audit;
- lint, typecheck, tests, build;
- migration SQL review and production-like rehearsal;
- database backup/restore proof;
- preview smoke, auth/RBAC, AI dry run, and provider dry run;
- nginx syntax and upstream health;
- public and origin TLS validation;
- rollback rehearsal;
- release manifest verification.

## Preview

Use an unexposed loopback port or Unix socket. Do not reuse port 8000. Do not add public DNS/nginx routing during foundation work. Preview configuration must force:

- external mutations disabled;
- media generation paid calls disabled;
- Exswaping mutation disabled;
- provider mocks or approved test accounts only;
- distinct database, queue, cache, cookie, storage, and logs.

## Production migration sequence

1. acquire deployment lock;
2. verify exact merged SHA and clean build context;
3. back up database and current release metadata;
4. validate additive migrations;
5. create immutable release and manifest;
6. start/health-check new service on its private upstream;
7. test authentication and authorization;
8. perform read-only and dry-run smoke tests;
9. back up nginx configuration;
10. validate candidate nginx configuration;
11. atomically select release/upstream;
12. reload nginx, never restart it unnecessarily;
13. observe errors, queue, scheduler, and database;
14. release deployment lock.

## Rollback

- switch `current` to the recorded prior immutable release;
- reload/restart only the dedicated application service;
- retain additive schema changes when backward compatible;
- never reverse a data-destructive migration blindly;
- disable workers before rollback if code/schema compatibility is uncertain;
- restore data only through a separately reviewed restore procedure;
- preserve failed release logs and manifest for audit.

## Current rollback blocker

AutoStory currently runs from a dirty source tree and has no immutable release target. A reliable code rollback cannot be asserted until lineage is captured. The recent SQLite recovery procedure is incident-specific and can restore malformed data; it is not an acceptable normal release rollback.
