"""Flask Dashboard module"""
from .app import create_app
from .routes import register_routes

__all__ = ["create_app", "register_routes"]
