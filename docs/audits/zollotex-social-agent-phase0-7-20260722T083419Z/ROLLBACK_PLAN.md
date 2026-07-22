# Rollback Plan

Rollback target: `/opt/autostory-releases/20260721T232351Z-499758e3465a`  
Backups: `/opt/autostory-phase0-7-evidence/20260722T083419Z/systemd-backup/`

```bash
cp /opt/autostory-phase0-7-evidence/20260722T083419Z/systemd-backup/web.override.conf \
  /etc/systemd/system/autostory-web.service.d/override.conf
cp /opt/autostory-phase0-7-evidence/20260722T083419Z/systemd-backup/scheduler.override.conf \
  /etc/systemd/system/autostory-scheduler.service.d/override.conf
rm -f /etc/systemd/system/autostory-readiness-worker.service.d/override.conf
# restore readiness unit from backup if needed
systemctl daemon-reload
systemctl restart autostory-scheduler autostory-readiness-worker autostory-web
ln -sfn /opt/autostory-releases/20260721T232351Z-499758e3465a /opt/autostory-releases/current
```

Verified prior release path exists. No DB downgrade required.
