"""
Gunicorn entry point; autostory-web.service uses: gunicorn ... wsgi:app
"""
from src.dashboard.app import create_app

app = create_app()
