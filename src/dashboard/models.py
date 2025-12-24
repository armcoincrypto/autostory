"""
Dashboard User Model for Flask-Login
"""
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import Column, Integer, String, Boolean, DateTime
from datetime import datetime

import sys
sys.path.insert(0, '/home/user/autostory')
from src.core.database import Base


class DashboardUser(Base, UserMixin):
    """Dashboard admin user"""
    __tablename__ = "dashboard_users"

    id = Column(Integer, primary_key=True)
    username = Column(String(80), unique=True, nullable=False)
    email = Column(String(120), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    is_admin = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_login = Column(DateTime, nullable=True)

    def set_password(self, password: str) -> None:
        """Hash and set password"""
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        """Check password against hash"""
        return check_password_hash(self.password_hash, password)

    def __repr__(self):
        return f'<DashboardUser {self.username}>'
