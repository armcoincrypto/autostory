"""
Human-facing error copy and safe Telegram error strings for API responses.

Never include secrets, session material, or API keys in returned text.
"""
from __future__ import annotations

import re
from typing import Any, Optional

# Redact path-like segments and very long tokens that might appear in Telethon traces.
_PATHISH = re.compile(r"(/[^\s]{3,200}|\\[^\s]{3,200})")
_LONG_TOKEN = re.compile(r"\b[A-Za-z0-9+/=_-]{80,}\b")


def sanitize_telegram_error_message(msg: Optional[str], max_len: int = 400) -> str:
    if not msg:
        return "Telegram reported an error."
    s = str(msg).strip().replace("\r", " ").replace("\n", " ")
    s = _PATHISH.sub("[path]", s)
    s = _LONG_TOKEN.sub("[redacted]", s)
    if len(s) > max_len:
        s = s[: max_len - 1] + "…"
    return s


def humanize_ai_agent_error(body: dict[str, Any]) -> str:
    """Single sentence for operators (English)."""
    err = str(body.get("error") or "error")
    detail = body.get("detail")
    code = body.get("error_code")
    parts: list[str] = []

    if err == "not_found":
        return "That task or account was not found."
    if err == "unauthorized":
        return "Sign in or provide a valid admin token to use the AI Agent API."
    if err == "forbidden_account":
        return (
            "This account is not reserved for AI Agent. Choose a dedicated AI Agent account."
        )
    if err == "account_not_allowed_for_ai_agent":
        return (
            "This account is not reserved for AI Agent. Use #110, #113, or #131."
        )
    if err == "database_busy":
        return "System is busy. Please retry in a few seconds."
    if err == "server_error":
        return str(detail or "Could not complete the request. Retry shortly.").strip()
    if err == "validation_error":
        return str(detail or "Some fields are missing or invalid.").strip()
    if err == "active_task_exists_for_target":
        return (
            "An active task already exists for this target. "
            "Open the existing task or pause it first."
        )
    if err == "invalid_state":
        return str(detail or "This action is not allowed for the task in its current state.").strip()
    if err == "no_draft":
        return "There is no AI draft to send. Generate a draft first, then approve."
    if err == "message_limit_reached":
        return str(
            detail or "This task has reached its outbound message limit (max_messages)."
        ).strip()
    if err == "telegram_fetch_failed":
        parts.append("Could not load messages from Telegram.")
        if code:
            parts.append(f"Code: {code}.")
        if body.get("error_message"):
            parts.append(sanitize_telegram_error_message(str(body["error_message"]), 200))
        return " ".join(parts).strip()
    if err == "send_failed":
        parts.append("Send did not complete.")
        if code:
            parts.append(f"Code: {code}.")
        if body.get("error_message"):
            parts.append(sanitize_telegram_error_message(str(body["error_message"]), 200))
        return " ".join(parts).strip()

    if detail:
        return str(detail)
    if code:
        return f"Request failed ({err}, code {code})."
    return f"Request failed ({err})."


def enrich_error_payload(body: dict[str, Any]) -> dict[str, Any]:
    out = dict(body)
    if "error" in out and "ok" not in out:
        out["ok"] = False
    if "human_message" not in out:
        out["human_message"] = humanize_ai_agent_error(out)
    if "error_message" in out and out["error_message"] is not None:
        out["error_message"] = sanitize_telegram_error_message(str(out["error_message"]))
    return out


def enrich_ai_agent_response(body: dict[str, Any]) -> dict[str, Any]:
    """Add human_message to error-shaped bodies or failed approve-send payloads."""
    b = dict(body)
    if "error" in b:
        return enrich_error_payload(b)
    if b.get("ok") is False:
        if b.get("transient"):
            b.setdefault(
                "human_message",
                "Telegram account is busy — retry later (another worker may hold the session).",
            )
        else:
            code = b.get("error_code") or "unknown"
            msg = sanitize_telegram_error_message(str(b.get("error_message") or ""))
            b.setdefault(
                "human_message",
                (f"Telegram send failed ({code}). {msg}").strip()
                if msg
                else f"Telegram send failed ({code}).",
            )
    elif b.get("ok") is True:
        b.setdefault(
            "human_message",
            "Message was delivered to Telegram as one manual send. Nothing else was sent automatically.",
        )
    return b
