"""
Gunicorn config for STORYFLEET Dashboard
Use: gunicorn -c deploy/gunicorn.conf.py "src.dashboard.app:create_app()"
"""
import multiprocessing
import os

# Bind
bind = os.environ.get("GUNICORN_BIND", "0.0.0.0:5000")
workers = int(os.environ.get("GUNICORN_WORKERS", 1))
worker_class = "sync"
timeout = 120

# Logging - ensure errors go to stderr for journalctl
accesslog = "-"  # stdout
errorlog = "-"  # stderr
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")
capture_output = True  # Capture Python stdout/stderr

# Path
chdir = os.environ.get("GUNICORN_CHDIR", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
