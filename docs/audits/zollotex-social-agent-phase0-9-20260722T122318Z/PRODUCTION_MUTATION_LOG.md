# Production Mutation Log

| UTC | ACTION | SERVICE | BEFORE | AFTER | MODE | DB_ROWS | FS | RESTARTED |
|---|---|---|---|---|---|---|---|---|
| 20260722T123107Z | promote_unified_release | web+scheduler+readiness | split 9b3ccc0/c9d1fe6 | 181c4731d404884b3c1453d0fcf9b1f83d67d13b @ /opt/autostory-releases/20260722T123044Z-181c4731d404 | disabled | 0 | 0 | yes |

Operator: phase0.9 agent

| 20260722T124714Z | install_prod_key | /etc/autostory/telegram-session-keys.env | none | prod-v1 present | disabled | 0 | 0 | yes |
| 20260722T124731Z | mode_transition | all three units | disabled | transition | transition | 0 | 0 | yes |
| 20260722T124751Z | db_backup | storyfleet.db | — | backup created | transition | 0 | 0 | no |
| 20260722T124806Z | migrate_sessions | storyfleet.db | 104 plain | 104 enc | transition | 104 session rows | 0 | no |
| 20260722T124825Z | quarantine_fs | data/sessions | 104 active | 0 active / 104 quarantined | transition | 0 | 104 moved | no |
| 20260722T124831Z | restart_services | web+scheduler+readiness | — | same SHA | transition | 0 | 0 | yes |

Completion state:
- Production runtime unified: **yes** (181c4731d404884b3c1453d0fcf9b1f83d67d13b)
- Production application code deployed: **yes**
- Production encryption key installed: **yes** (prod-v1)
- Production encryption mode changed: **yes** (disabled → transition)
- Production Telegram session rows changed: **yes** (104 migrated)
- Rows migrated: **104**
- Filesystem active sessions quarantined: **yes**
- Filesystem backups deleted: **no** (retained, restricted)
- Production FK rows repaired: **no**
- Gateway/Bot/Kathleen/AI loop/Scheduler lock: **unchanged safe**
- Social content / messages / funds: **no**
