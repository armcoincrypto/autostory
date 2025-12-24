"""
Flask Application Factory
STORYFLEET Control Dashboard
"""
from flask import Flask
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect
import structlog

import sys
sys.path.insert(0, '/home/user/autostory')
from config.settings import settings

logger = structlog.get_logger(__name__)

login_manager = LoginManager()
csrf = CSRFProtect()


def create_app() -> Flask:
    """Create and configure Flask application"""
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

    # Register blueprints
    from .routes import register_routes
    register_routes(app)

    # Error handlers
    @app.errorhandler(404)
    def not_found(error):
        return {"error": "Not found"}, 404

    @app.errorhandler(500)
    def internal_error(error):
        logger.error("Internal error", error=str(error))
        return {"error": "Internal server error"}, 500

    logger.info("Flask app created", environment=settings.environment)
    return app


@login_manager.user_loader
def load_user(user_id):
    """Load user for Flask-Login"""
    from .models import DashboardUser
    return DashboardUser.query.get(int(user_id))
