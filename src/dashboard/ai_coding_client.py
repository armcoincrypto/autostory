"""Server-side client for AI Software Factory review transparency API."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_BASE_URL = "http://127.0.0.1:8015"
TIMEOUT_SEC = 30


def ai_coding_api_base_url() -> str:
    return (os.environ.get("AI_CODING_API_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def _sanitize_health_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return only operator-safe health fields (no env, URLs, or secrets)."""
    services = payload.get("services")
    safe_services: dict[str, str] | None = None
    if isinstance(services, dict):
        safe_services = {
            str(key): str(value)
            for key, value in services.items()
            if isinstance(value, str)
        }
    return {
        "status": str(payload.get("status") or "unknown"),
        "app": str(payload.get("app") or "ai-software-factory"),
        "services": safe_services,
    }


def fetch_upstream_raw(
    path: str,
    *,
    query: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """Fetch upstream response as raw text (for markdown/file exports)."""
    base = ai_coding_api_base_url()
    url = f"{base}{path}"
    if query:
        params = urllib.parse.urlencode({k: v for k, v in query.items() if v is not None and v != ""})
        if params:
            url = f"{url}?{params}"
    request = urllib.request.Request(url, headers={"Accept": "*/*"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as response:
            body = response.read().decode("utf-8")
            content_type = response.headers.get("Content-Type", "text/plain")
            return response.getcode() or 200, body, content_type
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, body, exc.headers.get("Content-Type", "text/plain")
    except urllib.error.URLError as exc:
        return 502, json.dumps({"error": "ai_coding_upstream_unavailable", "detail": str(exc.reason)}), "application/json"
    except OSError as exc:
        return 502, json.dumps({"error": "ai_coding_upstream_unavailable", "detail": str(exc) or "connection failed"}), "application/json"


def fetch_upstream(path: str, *, query: dict[str, str] | None = None) -> tuple[int, Any]:
    """Call AI Coding API from server side. Never forwards browser secrets upstream."""
    return fetch_upstream_method("GET", path, query=query)


def fetch_upstream_method(
    method: str,
    path: str,
    *,
    query: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    base = ai_coding_api_base_url()
    url = f"{base}{path}"
    if query:
        params = urllib.parse.urlencode({k: v for k, v in query.items() if v is not None and v != ""})
        if params:
            url = f"{url}?{params}"

    data = None
    headers = {"Accept": "application/json"}
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as response:
            body = response.read().decode("utf-8")
            status = response.getcode() or 200
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        status = exc.code
    except urllib.error.URLError as exc:
        return 502, {
            "error": "ai_coding_upstream_unavailable",
            "detail": str(exc.reason),
        }
    except OSError as exc:
        return 502, {
            "error": "ai_coding_upstream_unavailable",
            "detail": str(exc) or "connection failed",
        }

    if not body.strip():
        return status, {}
    try:
        return status, json.loads(body)
    except json.JSONDecodeError:
        text = body.strip()[:500]
        if status >= 400:
            return status, {"detail": text or f"HTTP {status}"}
        return 502, {
            "error": "ai_coding_upstream_invalid_json",
            "detail": text,
        }


def fetch_upstream_health() -> tuple[int, dict[str, Any]]:
    """Probe upstream /health and return sanitized operator-safe status."""
    base = ai_coding_api_base_url()
    url = f"{base}/health"
    started = time.monotonic()
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read().decode("utf-8")
            http_status = response.getcode() or 200
    except urllib.error.HTTPError as exc:
        latency_ms = round((time.monotonic() - started) * 1000)
        return 503, {
            "connected": False,
            "status": "unavailable",
            "latency_ms": latency_ms,
            "error": "ai_coding_upstream_http_error",
            "http_status": exc.code,
        }
    except urllib.error.URLError:
        latency_ms = round((time.monotonic() - started) * 1000)
        return 503, {
            "connected": False,
            "status": "unavailable",
            "latency_ms": latency_ms,
            "error": "ai_coding_upstream_unavailable",
        }

    latency_ms = round((time.monotonic() - started) * 1000)
    if not body.strip():
        return 503, {
            "connected": False,
            "status": "unavailable",
            "latency_ms": latency_ms,
            "error": "ai_coding_upstream_empty_response",
        }
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return 503, {
            "connected": False,
            "status": "unavailable",
            "latency_ms": latency_ms,
            "error": "ai_coding_upstream_invalid_json",
        }

    if not isinstance(payload, dict):
        return 503, {
            "connected": False,
            "status": "unavailable",
            "latency_ms": latency_ms,
            "error": "ai_coding_upstream_invalid_json",
        }

    sanitized = _sanitize_health_payload(payload)
    connected = http_status == 200 and sanitized.get("status") in {"ok", "degraded"}
    return 200, {
        "connected": connected,
        "latency_ms": latency_ms,
        **sanitized,
    }
