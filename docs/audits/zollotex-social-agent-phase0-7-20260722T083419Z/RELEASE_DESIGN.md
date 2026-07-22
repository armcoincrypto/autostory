# Release Design

Release path: `/opt/autostory-releases/20260722T090829Z-c9d1fe614bb0`  
Method: `git archive` of exact SHA `c9d1fe614bb0b764805f532d9766bd35bbbb7ed0`  
Shared mutable refs: `/opt/autostory/.env`, `/opt/autostory/data`, `/opt/autostory/venv`  
Prohibited in release: `.git`, `.env`, databases, sessions, logs  
Scheduler singleton: release-local `deploy/run_scheduler_singleton.sh` (cwd = release root)
