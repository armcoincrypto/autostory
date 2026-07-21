# Runtime Process Map

Observed after the concurrent web promotion:

| Service | PID | Start | Working directory / entry | State | Role and lineage |
|---|---:|---|---|---|---|
| AutoStory web | 334298 | 2026-07-22 01:24:37 CEST | `/opt/autostory-releases/20260721T232351Z-499758e3465a`, `wsgi:app` | active | Immutable `git archive` release at `499758e`; one deleted-open descriptor; external `/opt/autostory` data/env/venv |
| AutoStory scheduler | 703773 | 2026-07-14 22:51:51 CEST | `/opt/autostory`, `main.py scheduler` | active, mutation locked | Dirty mutable tree; runtime source is not one commit |
| Readiness worker | 2907594 | 2026-07-18 06:44:13 CEST | `/opt/autostory/scripts/run_readiness_worker.py` | active | Dirty mutable tree; performs Telegram readiness probes |
| Swaperex admin | 318110 | 2026-07-22 01:07:10 CEST | `/root/Swaperex`, `swaperex.api.app_admin:app` | active | Restarted only for authorized token rotation |
| Telegram gateway | none | n/a | `/opt/autostory` | inactive | no messaging |
| Storyfleet bot | none | n/a | `/opt/autostory` | inactive | no messaging |
| Kathleen listener | none | n/a | `/opt/autostory` | inactive | rejected from candidate because source wrappers require untracked bytecode |

The web release manifest is `RELEASE_MANIFEST.json` and records commit `499758e`, `git archive`
creation, and external data/env/venv references. Scheduler and readiness remain the active
lineage blockers. No process was restarted merely to inspect it.
