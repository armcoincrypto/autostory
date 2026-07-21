# Secret Artifact Incident Inventory

## Scope and method

`scripts/audit/check_secret_artifacts.py` inventories names, metadata, variable names, categories, and one-way whole-file fingerprints. It never emits values. Restricted JSON evidence is stored outside Git.

## Counts

- Environment-like artifacts inspected under `/opt/autostory`: 25
- Real secret-bearing artifacts: 22
- Example/template artifacts: 3
- Git-tracked artifacts: 4 (three real backups and `.env.example`)
- Real artifacts initially mode `0644`: 21
- Real artifacts already mode `0600`: 1
- Real artifacts after containment with group/other access: 0
- Files whose permissions changed: 21
- Files moved/deleted/quarantined: 0

The 22 real artifacts consist of the active root `.env`, root-level operational backups, and backups under `data/audit`/`data/backups`. The full path/metadata list is retained in restricted machine evidence; no value is reproduced here.

## Tracked artifacts

| Path | First commit | Remote/tag reachability | Current mode | Status |
|---|---|---|---:|---|
| `.env.bak.2026-03-15_011640` | `99b04be8152419e24bfbea4e570f584f95a2e6c7` | remote production branch and tag `p14-2-auto-coding-ready-20260609` | `0600` | definitely exposed in Git |
| `.env.bak.2026-03-15_015848` | same | same | `0600` | definitely exposed in Git |
| `.env.bak.2026-03-15_020005` | same | same | `0600` | definitely exposed in Git |
| `.env.example` | tracked template | normal source file | `0644` | template; scan values before release but do not treat placeholders as credentials |

The introducing commit is reachable from the current production branch, Phase 0/0.5 branches, ten AI-build branches, the remote production branch, and one tag.

## Exposure categories

The tracked backups contain variable names associated with:

- Telegram bot credentials;
- Telegram API ID/hash;
- dashboard/operator token;
- database configuration;
- Redis configuration;
- storage paths/configuration;
- scheduler/application settings.

Comparison was performed in memory without printing values. Active-value overlap proves that `BOT_TOKEN`, `DASHBOARD_ADMIN_TOKEN` (in two backups), `TELEGRAM_API_ID`, and `TELEGRAM_API_HASH` remain active and must be treated as definitely exposed. Database URL and Redis entries also overlap but may be non-secret local configuration; they still require owner review.

## Immediate containment

All 21 insecure real artifacts were changed from `0644` to `0600`. Active services run as root and retained read access. A non-secret settings import and `systemd-analyze verify` passed. No service was restarted and no credential was changed.

This is containment, not incident closure. Git history and any clones/caches still contain the tracked values.

## Phase 0.5 evidence-capture incident

A read-only `systemctl show` command requested full service environment metadata and emitted the configured Swaperex `ADMIN_API_TOKEN` plus private account identifiers into agent tool output. Values are not reproduced in any audit document. The capture cannot be proven retractable, so the admin token is now classified definitely exposed and requires urgent Swaperex-owner rotation. No shell command contained the literal value and no runtime configuration was changed.
