#!/usr/bin/env python3
"""WSGI entry point for gunicorn."""
import sys
import os

# Project root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.dashboard.app import create_app

app = create_app()
