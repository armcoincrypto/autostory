# Deployment Log

| UTC | Service | Action | Notes |
|---|---|---|---|
| 20260722T091444Z | scheduler | restart → release | cwd release; mutations locked |
| 20260722T091444Z | readiness | restart → release | encryption error until mode=disabled |
| 20260722T091444Z | web | restart → release | AI loop started (inherited env true) |
| 20260722T091528Z | web | remediate | AI_AGENT_AUTO_LOOP_ENABLED=false; restart |
| 20260722T091623Z | all three | restart | TELEGRAM_SESSION_ENCRYPTION_MODE=disabled (no migration) |

Release: `/opt/autostory-releases/20260722T090829Z-c9d1fe614bb0`  
Rollback backups: `/opt/autostory-phase0-7-evidence/20260722T083419Z/systemd-backup/`
