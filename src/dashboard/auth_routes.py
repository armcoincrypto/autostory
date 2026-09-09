"""
Dashboard admin login/logout. Separate from Telegram account auth (/api/accounts/auth/*).
"""
import logging

from flask import Blueprint, redirect, render_template, request, url_for
from flask_login import login_user, logout_user, current_user

from src.core.database import get_db_context
from src.dashboard.models import DashboardUser
from src.dashboard.security_hardening import (
    clear_login_failures,
    login_rate_limited,
    record_login_failure,
)

logger = logging.getLogger(__name__)

auth = Blueprint("auth", __name__)


def _client_ip() -> str:
    # ProxyFix already rewrites remote_addr from X-Forwarded-For when trusted.
    return (request.remote_addr or "unknown").strip() or "unknown"


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
    next_url = request.form.get("next") or "/"
    ip = _client_ip()

    blocked, retry_after = login_rate_limited(ip=ip, username=username)
    if blocked:
        logger.warning(
            "dashboard_login_rate_limited ip=%s username=%s retry_after_sec=%s",
            ip,
            username or "(empty)",
            retry_after,
        )
        return (
            render_template(
                "login.html",
                error="Too many failed login attempts. Try again in a few minutes.",
                next_url=next_url,
            ),
            429,
        )

    if not username or not password:
        return render_template(
            "login.html",
            error="Username and password required",
            next_url=next_url,
        )

    with get_db_context() as db:
        user = db.query(DashboardUser).filter(DashboardUser.username == username).first()
        if not user or not user.check_password(password):
            record_login_failure(ip=ip, username=username)
            # Never log password or password length.
            logger.warning(
                "dashboard_login_failed ip=%s username=%s reason=invalid_credentials",
                ip,
                username,
            )
            return render_template(
                "login.html",
                error="Invalid username or password",
                next_url=next_url,
            )
        if not getattr(user, "is_active", True):
            record_login_failure(ip=ip, username=username)
            logger.warning(
                "dashboard_login_failed ip=%s username=%s reason=disabled",
                ip,
                username,
            )
            return render_template(
                "login.html",
                error="Account disabled",
                next_url=next_url,
            )
        if not getattr(user, "is_admin", False):
            record_login_failure(ip=ip, username=username)
            logger.warning(
                "dashboard_login_failed ip=%s username=%s reason=not_admin",
                ip,
                username,
            )
            return render_template(
                "login.html",
                error="Access denied",
                next_url=next_url,
            )
        clear_login_failures(ip=ip, username=username)
        # Remember-me stays on for owner convenience; cookie flags set in create_app.
        login_user(user, remember=True)
        logger.info("dashboard_login_ok ip=%s username=%s", ip, username)

    next_url = (request.form.get("next") or request.args.get("next") or "/").strip()
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = "/"
    return redirect(next_url)


@auth.route("/logout", methods=["GET", "POST"])
def logout():
    """Log out admin and redirect to login.

    POST is preferred (CSRF-protected). GET remains for older bookmarks but
    SameSite=Lax session cookies limit cross-site GET logout abuse.
    """
    if request.method == "GET" and current_user.is_authenticated:
        logger.info("dashboard_logout_via_get")
    logout_user()
    return redirect(url_for("auth.login"))
