"""Shared fail-closed authorization for dashboard and operator JSON APIs."""

from __future__ import annotations

import hmac
import os

import structlog
from flask import request
from flask_login import current_user


logger = structlog.get_logger(__name__)


def _configured_admin_token() -> str:
    return os.environ.get("DASHBOARD_ADMIN_TOKEN", "") or ""


def _header_token_matches() -> bool:
    expected = _configured_admin_token()
    provided = (request.headers.get("X-Admin-Token") or "").strip()
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def dashboard_api_authorized() -> bool:
    """Preserve dashboard compatibility while failing closed when no auth exists."""
    if current_user.is_authenticated:
        return getattr(current_user, "is_admin", None) is not False

    token_matched = _header_token_matches()
    if not token_matched:
        allow_query = os.environ.get(
            "DASHBOARD_ALLOW_QUERY_ADMIN_TOKEN", "false"
        ).strip().lower() in {"1", "true", "yes", "on"}
        expected = _configured_admin_token()
        query_token = (request.args.get("admin_token") or "").strip() if allow_query else ""
        token_matched = bool(
            expected
            and query_token
            and hmac.compare_digest(
                query_token.encode("utf-8"), expected.encode("utf-8")
            )
        )

    if os.environ.get("DASHBOARD_API_AUTH_DEBUG", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        logger.info(
            "dashboard_api_auth_debug",
            endpoint=getattr(request, "endpoint", None),
            path=request.path,
            is_authenticated=bool(current_user.is_authenticated),
            token_matched=token_matched,
            authorized=token_matched,
        )
    return token_matched


def operator_api_authorized() -> bool:
    """Authorize restricted diagnostics.

    Query-string tokens are intentionally never accepted. A session must carry
    an explicit administrator role; legacy NULL role values are insufficient.
    """
    if current_user.is_authenticated and getattr(current_user, "is_admin", False) is True:
        return True
    return _header_token_matches()
