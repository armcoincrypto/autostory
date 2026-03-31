"""
Flask Application Factory
STORYFLEET Control Dashboard
"""
import os
import sys
import traceback

from flask import Flask
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect
import structlog

# Add project root to path (works for both /home/user/autostory and /opt/autostory)
_script_dir = os.path.dirname(os.path.abspath(__file__))
_project_root = os.path.abspath(os.path.join(_script_dir, "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from config.settings import settings
from src.core.database import init_db

logger = structlog.get_logger(__name__)

login_manager = LoginManager()
csrf = CSRFProtect()


def create_app() -> Flask:
    """Create and configure Flask application"""
    # Gunicorn uses this factory directly (not main.py), so ensure DB schema matches models.
    init_db()
    app = Flask(
        __name__,
        template_folder='templates',
        static_folder='static'
    )

    # Configuration
    app.config['SECRET_KEY'] = settings.dashboard.secret_key
    app.config['SQLALCHEMY_DATABASE_URI'] = settings.database.url
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    # Initialize extensions
    login_manager.init_app(app)
    login_manager.login_view = 'auth.login'
    csrf.init_app(app)

    from .auth_routes import auth
    app.register_blueprint(auth)

    # Register blueprints
    from .routes import register_routes, api
    from .scheduler_routes import scheduler_api
    register_routes(app)
    app.register_blueprint(scheduler_api)
    csrf.exempt(api)
    csrf.exempt(scheduler_api)

    # Error handlers
    @app.errorhandler(404)
    def not_found(error):
        return {"error": "Not found"}, 404

    @app.errorhandler(500)
    def internal_error(error):
        tb = traceback.format_exc()
        logger.error("Internal server error", error=str(error), traceback=tb)
        # Also print to stderr so journalctl captures it
        import sys
        print(traceback.format_exc(), file=sys.stderr, flush=True)
        return {"error": "Internal server error"}, 500

    logger.info("Flask app created", environment=settings.environment)
    return app


@login_manager.user_loader
def load_user(user_id):
    """Load user for Flask-Login (uses raw SQLAlchemy, not Flask-SQLAlchemy)"""
    try:
        from .models import DashboardUser
        from src.core.database import get_db_context
        with get_db_context() as db:
            return db.get(DashboardUser, int(user_id))
    except Exception:
        return None
