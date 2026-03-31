"""
Dashboard admin login/logout. Separate from Telegram account auth (/api/accounts/auth/*).
"""
from flask import Blueprint, redirect, render_template, request, url_for
from flask_login import login_user, logout_user, current_user

from src.core.database import get_db_context
from src.dashboard.models import DashboardUser

auth = Blueprint("auth", __name__)


@auth.route("/login", methods=["GET", "POST"])
def login():
    """Admin login: GET shows form, POST verifies credentials and logs in."""
    if current_user.is_authenticated and getattr(current_user, "is_admin", False):
        return redirect(request.args.get("next") or "/")
    if request.method == "GET":
        next_url = request.args.get("next") or "/"
        return render_template("login.html", next_url=next_url)
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    if not username or not password:
        return render_template("login.html", error="Username and password required", next_url=request.form.get("next") or "/")
    with get_db_context() as db:
        user = db.query(DashboardUser).filter(DashboardUser.username == username).first()
        if not user or not user.check_password(password):
            return render_template("login.html", error="Invalid username or password", next_url=request.form.get("next") or "/")
        if not getattr(user, "is_active", True):
            return render_template("login.html", error="Account disabled", next_url=request.form.get("next") or "/")
        if not getattr(user, "is_admin", False):
            return render_template("login.html", error="Access denied", next_url=request.form.get("next") or "/")
        login_user(user, remember=True)
    next_url = (request.form.get("next") or request.args.get("next") or "/").strip()
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = "/"
    return redirect(next_url)


@auth.route("/logout")
def logout():
    """Log out admin and redirect to login."""
    logout_user()
    return redirect(url_for("auth.login"))
