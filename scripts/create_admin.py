#!/usr/bin/env python3
"""Create the first dashboard admin user. Run: python scripts/create_admin.py [username] [password]"""
import os
import sys

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _project_root)

# Load .env before any app imports
_env_path = os.path.join(_project_root, ".env")
if os.path.isfile(_env_path):
    try:
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=_env_path)
    except Exception:
        pass

from src.core.database import init_db, get_db_context
from src.dashboard.models import DashboardUser


def main():
    init_db()
    username = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("ADMIN_USER", "admin")).strip()
    password = (sys.argv[2] if len(sys.argv) > 2 else os.environ.get("ADMIN_PASSWORD", "")).strip()

    if not password:
        print("Usage: python scripts/create_admin.py <username> <password>")
        print("   or: ADMIN_USER=admin ADMIN_PASSWORD=secret python scripts/create_admin.py")
        sys.exit(1)

    with get_db_context() as db:
        existing = db.query(DashboardUser).filter(DashboardUser.username == username).first()
        if existing:
            existing.set_password(password)
            existing.is_admin = True
            existing.is_active = True
            db.commit()
            print(f"Updated password for existing admin: {username}")
        else:
            user = DashboardUser(
                username=username,
                email=f"{username}@local",
                is_admin=True,
                is_active=True,
            )
            user.set_password(password)
            db.add(user)
            db.commit()
            print(f"Created admin user: {username}")
    print("You can now log in at /login")


if __name__ == "__main__":
    main()
