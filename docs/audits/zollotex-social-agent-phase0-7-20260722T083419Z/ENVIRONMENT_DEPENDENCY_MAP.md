# Environment Dependency Map

| Dependency | Location | Notes |
|---|---|---|
| Source | immutable release | git archive of c9d1fe6 |
| Python venv | `/opt/autostory/venv` | shared external |
| Env file | `/opt/autostory/.env` | shared external |
| Database | `/opt/autostory/data` | symlink from release |
| Session encryption | mode=disabled via unit override | keys not configured; migration deferred |
