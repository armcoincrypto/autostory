"""Meta Graph API adapter — read-only OAuth, discovery, and health.

Graph API version verified from Meta docs (developers.facebook.com): v25.0.
Publishing endpoints are intentionally absent from this module.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlencode

GRAPH_API_VERSION = "v25.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
OAUTH_DIALOG = f"https://www.facebook.com/{GRAPH_API_VERSION}/dialog/oauth"

# Minimum scopes for read-only Page + Instagram Professional discovery.
# Publishing scopes are intentionally excluded in this phase.
READ_ONLY_SCOPES = (
    "pages_show_list",
    "pages_read_engagement",
    "instagram_basic",
)

HttpCaller = Callable[[str, str, dict[str, str] | None, bytes | None, float], tuple[int, dict[str, Any]]]


def _default_http(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 20.0,
) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(url, data=body, method=method.upper())
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw) if raw else {}
            return int(resp.status), data if isinstance(data, dict) else {"data": data}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {"error": {"message": "http_error", "code": exc.code}}
        if not isinstance(data, dict):
            data = {"error": {"message": "http_error", "code": exc.code}}
        return int(exc.code), data
    except Exception as exc:
        return 0, {"error": {"message": "network_error", "type": type(exc).__name__}}


@dataclass
class MetaProviderAdapter:
    """Meta-specific behavior only. Tokens never leave the service layer."""

    http: HttpCaller = _default_http
    api_version: str = GRAPH_API_VERSION

    @staticmethod
    def config() -> dict[str, Any]:
        app_id = (os.environ.get("META_APP_ID") or "").strip()
        secret = (os.environ.get("META_APP_SECRET") or "").strip()
        redirect = (os.environ.get("META_REDIRECT_URI") or "").strip()
        publishing_fb = (os.environ.get("META_FACEBOOK_PUBLISHING_ENABLED") or "false").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        publishing_ig = (os.environ.get("META_INSTAGRAM_PUBLISHING_ENABLED") or "false").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        app_mode = (os.environ.get("META_APP_MODE") or "unknown").strip() or "unknown"
        https_ok = redirect.startswith("https://")
        env = (os.environ.get("ENVIRONMENT") or os.environ.get("FLASK_ENV") or "production").strip().lower()
        redirect_ok = bool(redirect) and (https_ok or env in {"development", "dev", "test"})
        return {
            "app_id": app_id,
            "app_secret_present": bool(secret),
            "redirect_uri": redirect,
            "configured": bool(app_id and secret and redirect_ok),
            "redirect_https": https_ok,
            "api_version": GRAPH_API_VERSION,
            "scopes": list(READ_ONLY_SCOPES),
            "app_mode": app_mode,
            "facebook_publishing_enabled": publishing_fb,
            "instagram_publishing_enabled": publishing_ig,
        }

    def authorization_url(self, *, state: str) -> str:
        cfg = self.config()
        if not cfg["configured"]:
            raise RuntimeError("META_NOT_CONFIGURED")
        params = {
            "client_id": cfg["app_id"],
            "redirect_uri": cfg["redirect_uri"],
            "state": state,
            "response_type": "code",
            "scope": ",".join(READ_ONLY_SCOPES),
        }
        return f"{OAUTH_DIALOG}?{urlencode(params)}"

    def exchange_code(self, *, code: str) -> dict[str, Any]:
        cfg = self.config()
        if not cfg["configured"]:
            return {"ok": False, "error": "META_NOT_CONFIGURED"}
        secret = (os.environ.get("META_APP_SECRET") or "").strip()
        qs = urlencode(
            {
                "client_id": cfg["app_id"],
                "redirect_uri": cfg["redirect_uri"],
                "client_secret": secret,
                "code": code,
            }
        )
        status, data = self.http("GET", f"{GRAPH_BASE}/oauth/access_token?{qs}", None, None, 20.0)
        if status != 200 or not data.get("access_token"):
            return {"ok": False, "error": "token_exchange_failed", "status": status, "category": _error_category(data)}
        return {
            "ok": True,
            "access_token": data["access_token"],
            "token_type": data.get("token_type") or "bearer",
            "expires_in": data.get("expires_in"),
        }

    def exchange_long_lived(self, *, short_lived_token: str) -> dict[str, Any]:
        cfg = self.config()
        secret = (os.environ.get("META_APP_SECRET") or "").strip()
        qs = urlencode(
            {
                "grant_type": "fb_exchange_token",
                "client_id": cfg["app_id"],
                "client_secret": secret,
                "fb_exchange_token": short_lived_token,
            }
        )
        status, data = self.http("GET", f"{GRAPH_BASE}/oauth/access_token?{qs}", None, None, 20.0)
        if status != 200 or not data.get("access_token"):
            return {"ok": False, "error": "long_lived_exchange_failed", "status": status, "category": _error_category(data)}
        return {
            "ok": True,
            "access_token": data["access_token"],
            "token_type": data.get("token_type") or "bearer",
            "expires_in": data.get("expires_in"),
        }

    def debug_token(self, *, input_token: str) -> dict[str, Any]:
        cfg = self.config()
        secret = (os.environ.get("META_APP_SECRET") or "").strip()
        app_token = f"{cfg['app_id']}|{secret}"
        qs = urlencode({"input_token": input_token, "access_token": app_token})
        status, data = self.http("GET", f"{GRAPH_BASE}/debug_token?{qs}", None, None, 20.0)
        if status != 200:
            return {"ok": False, "error": "debug_token_failed", "status": status, "category": _error_category(data)}
        return {"ok": True, "data": (data.get("data") or {})}

    def get_me(self, *, access_token: str) -> dict[str, Any]:
        qs = urlencode({"fields": "id,name", "access_token": access_token})
        status, data = self.http("GET", f"{GRAPH_BASE}/me?{qs}", None, None, 20.0)
        if status != 200 or not data.get("id"):
            return {"ok": False, "error": "me_failed", "status": status, "category": _error_category(data)}
        return {"ok": True, "id": str(data["id"]), "name": data.get("name")}

    def list_pages(self, *, access_token: str) -> dict[str, Any]:
        """Discover Pages; paginate; never return page tokens to callers outside services."""
        pages: list[dict[str, Any]] = []
        url = f"{GRAPH_BASE}/me/accounts?{urlencode({'fields': 'id,name,category,tasks,access_token,instagram_business_account{id,username}', 'limit': '50', 'access_token': access_token})}"
        guard = 0
        while url and guard < 20:
            guard += 1
            status, data = self.http("GET", url, None, None, 20.0)
            if status != 200:
                return {"ok": False, "error": "pages_failed", "status": status, "category": _error_category(data), "pages": pages}
            for row in data.get("data") or []:
                ig = row.get("instagram_business_account") or {}
                pages.append(
                    {
                        "page_id": str(row.get("id") or ""),
                        "page_name": row.get("name") or "",
                        "category": row.get("category"),
                        "tasks": row.get("tasks") or [],
                        "page_access_token": row.get("access_token"),  # service-only
                        "instagram_account_id": str(ig.get("id")) if ig.get("id") else None,
                        "instagram_username": ig.get("username"),
                    }
                )
            paging = data.get("paging") or {}
            url = paging.get("next")
        return {"ok": True, "pages": pages}

    def page_instagram(self, *, page_id: str, page_access_token: str) -> dict[str, Any]:
        qs = urlencode(
            {
                "fields": "instagram_business_account{id,username,account_type}",
                "access_token": page_access_token,
            }
        )
        status, data = self.http("GET", f"{GRAPH_BASE}/{urllib.parse.quote(page_id)}?{qs}", None, None, 20.0)
        if status != 200:
            return {"ok": False, "error": "instagram_lookup_failed", "status": status, "category": _error_category(data)}
        ig = data.get("instagram_business_account") or {}
        if not ig.get("id"):
            return {"ok": True, "instagram": None}
        return {
            "ok": True,
            "instagram": {
                "instagram_account_id": str(ig["id"]),
                "username": ig.get("username"),
                "account_type": ig.get("account_type"),
                "linked_page_id": str(page_id),
            },
        }

    def health_probe(self, *, access_token: str, page_id: str | None = None) -> dict[str, Any]:
        me = self.get_me(access_token=access_token)
        if not me.get("ok"):
            return {"ok": False, "health": "ERROR", "reason": me.get("category") or me.get("error")}
        if page_id:
            qs = urlencode({"fields": "id,name", "access_token": access_token})
            status, data = self.http("GET", f"{GRAPH_BASE}/{urllib.parse.quote(page_id)}?{qs}", None, None, 20.0)
            if status != 200:
                return {"ok": False, "health": "PAGE_UNAVAILABLE", "reason": _error_category(data)}
        return {"ok": True, "health": "CONNECTED", "user_id": me.get("id")}


def _error_category(data: dict[str, Any]) -> str:
    err = data.get("error") if isinstance(data, dict) else None
    if not isinstance(err, dict):
        return "provider_error"
    code = err.get("code")
    sub = err.get("error_subcode")
    msg = str(err.get("message") or "").lower()
    if code in {190} or "session has expired" in msg or "expired" in msg:
        return "EXPIRED"
    if code in {10, 200, 294} or "permission" in msg:
        return "PERMISSION_MISSING"
    if "revok" in msg:
        return "REVOKED"
    if sub:
        return f"provider_{code}_{sub}"
    return f"provider_{code}" if code is not None else "provider_error"


def capability_matrix(granted: list[str] | None = None) -> list[dict[str, Any]]:
    granted_set = {g.strip() for g in (granted or [])}
    rows = [
        ("authenticate_user", "public_profile", False, True, True),
        ("list_pages", "pages_show_list", False, True, True),
        ("read_page_metadata", "pages_read_engagement", False, True, True),
        ("discover_instagram", "instagram_basic", False, True, True),
        ("connection_health", "pages_show_list", False, True, True),
        ("future_facebook_publishing", "pages_manage_posts", True, False, False),
        ("future_instagram_publishing", "instagram_content_publish", True, False, False),
        ("future_analytics", "instagram_manage_insights", True, False, False),
        ("future_comments", "instagram_manage_comments", True, False, False),
        ("future_messages", "instagram_manage_messages", True, False, False),
    ]
    out = []
    for cap, perm, review, dev, prod in rows:
        out.append(
            {
                "capability": cap,
                "required_permission": perm,
                "granted": perm in granted_set or (cap == "authenticate_user"),
                "app_review_required": review,
                "development_mode_allowed": dev,
                "production_mode_allowed": prod and not review,
            }
        )
    return out
