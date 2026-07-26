"""Meta Graph API adapter — OAuth, discovery, health, and gated Page publish.

Graph API version verified from Meta docs (developers.facebook.com): v25.0.
Facebook Page publish requires a valid CanaryAuthorization object.
Instagram publish methods are intentionally absent.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, TYPE_CHECKING
from urllib.parse import urlencode

if TYPE_CHECKING:
    from src.social_agent.publishing.canary_auth import CanaryAuthorization

GRAPH_API_VERSION = "v25.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
OAUTH_DIALOG = f"https://www.facebook.com/{GRAPH_API_VERSION}/dialog/oauth"

# Minimum scopes for read-only Page + Instagram Professional discovery.
READ_ONLY_SCOPES = (
    "pages_show_list",
    "pages_read_engagement",
    "instagram_basic",
)

# Additional scope required only for Facebook controlled-canary reconnect.
FACEBOOK_CANARY_SCOPES = READ_ONLY_SCOPES + ("pages_manage_posts",)

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
            "canary_scopes": list(FACEBOOK_CANARY_SCOPES),
            "app_mode": app_mode,
            "facebook_publishing_enabled": publishing_fb,
            "instagram_publishing_enabled": publishing_ig,
            "publishing_execution_mode": (os.environ.get("META_PUBLISHING_EXECUTION_MODE") or "disabled").strip().lower(),
        }

    def authorization_url(self, *, state: str, purpose: str = "connect") -> str:
        cfg = self.config()
        if not cfg["configured"]:
            raise RuntimeError("META_NOT_CONFIGURED")
        # Reconnect in this phase also requests pages_manage_posts for the Facebook canary.
        scopes = (
            FACEBOOK_CANARY_SCOPES
            if purpose in {"publish_canary", "facebook_canary", "reconnect"}
            else READ_ONLY_SCOPES
        )
        params = {
            "client_id": cfg["app_id"],
            "redirect_uri": cfg["redirect_uri"],
            "state": state,
            "response_type": "code",
            "scope": ",".join(scopes),
        }
        if purpose in {"publish_canary", "facebook_canary", "reconnect"}:
            params["auth_type"] = "rerequest"
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

    @staticmethod
    def _page_from_graph_row(row: dict[str, Any]) -> dict[str, Any]:
        ig_biz = row.get("instagram_business_account") or {}
        ig_conn = row.get("connected_instagram_account") or {}
        ig = ig_biz if ig_biz.get("id") else ig_conn
        return {
            "page_id": str(row.get("id") or ""),
            "page_name": row.get("name") or "",
            "category": row.get("category"),
            "tasks": row.get("tasks") or [],
            "page_access_token": row.get("access_token"),
            "instagram_account_id": str(ig.get("id")) if ig.get("id") else None,
            "instagram_username": ig.get("username"),
        }

    def list_pages(self, *, access_token: str) -> dict[str, Any]:
        """Discover Pages; fall back to debug_token granular target_ids when /me/accounts is empty."""
        page_fields = (
            "id,name,category,tasks,access_token,"
            "instagram_business_account{id,username},"
            "connected_instagram_account{id,username}"
        )
        pages: list[dict[str, Any]] = []
        url = f"{GRAPH_BASE}/me/accounts?{urlencode({'fields': page_fields, 'limit': '50', 'access_token': access_token})}"
        guard = 0
        accounts_ok = True
        while url and guard < 20:
            guard += 1
            status, data = self.http("GET", url, None, None, 20.0)
            if status != 200:
                accounts_ok = False
                break
            for row in data.get("data") or []:
                parsed = self._page_from_graph_row(row)
                if parsed.get("page_id"):
                    pages.append(parsed)
            paging = data.get("paging") or {}
            url = paging.get("next")

        discovery_source = "me_accounts"
        if accounts_ok and not pages:
            debug = self.debug_token(input_token=access_token)
            if not debug.get("ok"):
                return {
                    "ok": False,
                    "error": "pages_failed",
                    "category": debug.get("category") or "debug_token_failed",
                    "pages": [],
                    "discovery_source": "granular_scopes",
                }
            page_ids: list[str] = []
            for entry in (debug.get("data") or {}).get("granular_scopes") or []:
                if not isinstance(entry, dict):
                    continue
                if str(entry.get("scope") or "") not in {
                    "pages_show_list",
                    "pages_read_engagement",
                    "pages_manage_posts",
                }:
                    continue
                for tid in entry.get("target_ids") or []:
                    sid = str(tid)
                    if sid and sid not in page_ids:
                        page_ids.append(sid)
            for page_id in page_ids:
                fetched = self.get_page(page_id=page_id, access_token=access_token)
                if fetched.get("ok") and fetched.get("page"):
                    pages.append(fetched["page"])
            discovery_source = "granular_scopes"

        if not accounts_ok and not pages:
            return {
                "ok": False,
                "error": "pages_failed",
                "category": "provider_error",
                "pages": pages,
                "discovery_source": discovery_source,
            }
        return {"ok": True, "pages": pages, "discovery_source": discovery_source}

    def get_page(self, *, page_id: str, access_token: str) -> dict[str, Any]:
        qs = urlencode(
            {
                "fields": (
                    "id,name,category,tasks,access_token,"
                    "instagram_business_account{id,username},"
                    "connected_instagram_account{id,username}"
                ),
                "access_token": access_token,
            }
        )
        status, data = self.http("GET", f"{GRAPH_BASE}/{urllib.parse.quote(page_id)}?{qs}", None, None, 20.0)
        if status != 200 or not data.get("id"):
            return {"ok": False, "error": "page_lookup_failed", "status": status, "category": _error_category(data)}
        return {"ok": True, "page": self._page_from_graph_row(data)}

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

    def list_user_permissions(self, *, access_token: str) -> dict[str, Any]:
        qs = urlencode({"access_token": access_token})
        status, data = self.http("GET", f"{GRAPH_BASE}/me/permissions?{qs}", None, None, 20.0)
        if status != 200:
            return {"ok": False, "error": "permissions_failed", "status": status, "category": _error_category(data)}
        granted = [
            str(row.get("permission"))
            for row in (data.get("data") or [])
            if row.get("status") == "granted" and row.get("permission")
        ]
        return {"ok": True, "granted": granted}

    def publish_page_feed(
        self,
        *,
        page_id: str,
        page_access_token: str,
        message: str,
        authorization: "CanaryAuthorization | None",
        facebook_gate_enabled: bool,
        execution_mode: str,
    ) -> dict[str, Any]:
        """POST /{page-id}/feed — requires a valid canary authorization object."""
        if authorization is None:
            return {
                "ok": False,
                "error": "CANARY_AUTHORIZATION_REQUIRED",
                "category": "NOT_AUTHORIZED",
                "provider_called": False,
                "http_posts": 0,
            }
        if not facebook_gate_enabled:
            return {
                "ok": False,
                "error": "FACEBOOK_PUBLISHING_DISABLED",
                "category": "NOT_AUTHORIZED",
                "provider_called": False,
                "http_posts": 0,
            }
        if execution_mode != "controlled-canary":
            return {
                "ok": False,
                "error": "EXECUTION_MODE_DENIED",
                "category": "NOT_AUTHORIZED",
                "provider_called": False,
                "http_posts": 0,
                "message": "Only controlled-canary execution mode is allowed in this phase.",
            }
        if str(page_id) != str(authorization.page_id):
            return {
                "ok": False,
                "error": "PAGE_MISMATCH",
                "category": "NOT_AUTHORIZED",
                "provider_called": False,
                "http_posts": 0,
            }
        if authorization.destination != "facebook_page":
            return {
                "ok": False,
                "error": "DESTINATION_DENIED",
                "category": "NOT_AUTHORIZED",
                "provider_called": False,
                "http_posts": 0,
            }
        body = urlencode({"message": message, "access_token": page_access_token}).encode("utf-8")
        url = f"{GRAPH_BASE}/{urllib.parse.quote(str(page_id))}/feed"
        status, data = self.http(
            "POST",
            url,
            {"Content-Type": "application/x-www-form-urlencoded"},
            body,
            30.0,
        )
        safe_data = {k: v for k, v in (data or {}).items() if "token" not in str(k).lower()}
        if status != 200 or not safe_data.get("id"):
            return {
                "ok": False,
                "error": "publish_failed",
                "status": status,
                "category": _error_category(data if isinstance(data, dict) else {}),
                "provider_called": True,
                "http_posts": 1,
                "provider_request_id": (data.get("error") or {}).get("fbtrace_id") if isinstance(data, dict) else None,
                "response": safe_data,
            }
        post_id = str(safe_data["id"])
        return {
            "ok": True,
            "external_post_id": post_id,
            "status": status,
            "category": "SUCCEEDED",
            "provider_called": True,
            "http_posts": 1,
            "provider_request_id": None,
            "graph_api_version": GRAPH_API_VERSION,
            "endpoint": f"/{page_id}/feed",
            "public_url": f"https://www.facebook.com/{post_id}",
            "response": {"id": post_id},
        }

    def delete_page_post(
        self,
        *,
        post_id: str,
        page_access_token: str,
        authorization: "CanaryAuthorization | None",
        explicit_delete_approval: str,
    ) -> dict[str, Any]:
        if authorization is None:
            return {"ok": False, "error": "CANARY_AUTHORIZATION_REQUIRED", "provider_called": False}
        if explicit_delete_approval != "CONFIRM_DELETE":
            return {"ok": False, "error": "delete_not_approved", "provider_called": False}
        qs = urlencode({"access_token": page_access_token})
        status, data = self.http(
            "DELETE",
            f"{GRAPH_BASE}/{urllib.parse.quote(str(post_id))}?{qs}",
            None,
            None,
            20.0,
        )
        return {
            "ok": status == 200 and bool((data or {}).get("success")),
            "status": status,
            "provider_called": True,
            "category": "DELETED" if status == 200 else _error_category(data if isinstance(data, dict) else {}),
            "response": {k: v for k, v in (data or {}).items() if "token" not in str(k).lower()},
        }


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
        ("facebook_page_publishing", "pages_manage_posts", True, True, False),
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
