# Scheduler / Readiness Parity

Both services now share with web:

- WorkingDirectory = `/opt/autostory-releases/20260722T090829Z-c9d1fe614bb0`
- PYTHONPATH = release path
- shared `.env` + venv + data
- SCHEDULER_MUTATIONS_ENABLED=false
- TELEGRAM_SESSION_ENCRYPTION_MODE=disabled (plaintext read compatibility; no migration)
- AI_AGENT_AUTO_LOOP_ENABLED=false

No process cwd points at dirty `/opt/autostory` source.
