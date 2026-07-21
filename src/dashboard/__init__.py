"""Flask Dashboard module."""
from .app import create_app

try:
    from .routes import register_routes
except (ModuleNotFoundError, ImportError):

    def register_routes(app):  # type: ignore[no-redef]
        """No-op when legacy routes module is absent (bytecode-only deploy)."""
        return None

__all__ = ["create_app", "register_routes"]
