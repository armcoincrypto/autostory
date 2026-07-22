# Filesystem Session Retirement

| Item | Result |
|---|---|
| Active `.session` before | 104 |
| Active `.session` after | 0 |
| Quarantined | 104 (0600) into `/opt/autostory/data/session_quarantine/phase0-9-<ts>/` |
| Runtime dependency | 0 (resolve uses encrypted StringSession only) |
| Backup `.session` files | 511 retained, chmod 600, not deleted |
| Deletion | pending owner approval after encrypted-only soak |
