"""Owner Messages Send-Now — thin authenticated page + API (Wave 7).

Business logic lives in ``OwnerDirectMessageService`` / messaging primitives.
Routes only authenticate, parse, call, and normalize responses.
"""
from __future__ import annotations

from typing import Any, Optional

import structlog
from flask import Blueprint, jsonify, redirect, render_template, request, url_for
from flask_login import login_required

from src.clients.telethon_runtime import run as telethon_run
from src.core.database import get_db_context
from src.core.models import Account
from src.dashboard.auth_access import dashboard_api_authorized
from src.messaging.claude_draft_flags import claude_draft_enabled
from src.messaging.claude_draft_service import ClaudeDraftService
from src.messaging.eligibility import evaluate_dm_account_eligibility
from src.messaging.flags import messages_execution_enabled
from src.messaging.models import OwnerDmIntent
from src.messaging.owner_dm_service import OwnerDirectMessageService
from src.messaging.transport import TelegramDmTransport

logger = structlog.get_logger(__name__)

messages_bp = Blueprint("messages", __name__)
messages_api = Blueprint("messages_api", __name__, url_prefix="/api/messages")


def _run_async(coro):
    """Same Telethon runtime as dialogs (routes.run_async) — Wave 7A."""
    return telethon_run(coro)


def _deny_unauthorized():
    return jsonify({"ok": False, "error": "unauthorized"}), 401


@messages_bp.before_request
def _messages_page_auth():
    if request.endpoint == "messages.messages_page":
        if not dashboard_api_authorized():
            return redirect(url_for("auth.login", next=request.full_path or request.path))
    return None


@messages_api.before_request
def _messages_api_auth():
    if not dashboard_api_authorized():
        return _deny_unauthorized()
    return None


def _dt_iso(value) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        s = value.isoformat()
        if not s.endswith("Z") and "+" not in s:
            return s + "Z"
        return s
    return str(value)


def _intent_owner_safe(
    intent: OwnerDmIntent,
    *,
    account_label: Optional[str] = None,
    include_ok: bool = True,
) -> dict[str, Any]:
    peer_display = (intent.peer_username and f"@{intent.peer_username}") or intent.peer_id
    row = {
        "intent_id": intent.id,
        "idempotency_key": intent.idempotency_key,
        "status": intent.status,
        "account_id": intent.account_id,
        "account_label": account_label or f"Account #{intent.account_id}",
        "peer": intent.peer_id,
        "peer_display": peer_display,
        "peer_type": intent.peer_type,
        "created_at": _dt_iso(intent.created_at),
        "attempted_at": _dt_iso(intent.attempt_started_at),
        "sent_at": _dt_iso(intent.sent_at),
        "telegram_message_id": intent.telegram_message_id,
        "error_code": intent.error_code,
        "error_summary": intent.error_message,
        "message_preview": intent.message_preview,
        "retry_after": None,
    }
    if include_ok:
        row["ok"] = True
    return row


_ACCOUNT_DENY_CODES = frozenset(
    {
        "ACCOUNT_PROTECTED",
        "ACCOUNT_RESERVED",
        "ACCOUNT_DISABLED",
        "ACCOUNT_INELIGIBLE",
        "AUTH_REQUIRED",
    }
)


@messages_bp.route("/messages")
@login_required
def messages_page():
    """Owner Messages Send-Now UI (Telegram live send gated by kill switch)."""
    return render_template(
        "messages.html",
        messages_execution_enabled=messages_execution_enabled(),
    )


@messages_api.route("/status", methods=["GET"])
def messages_status():
    """Kill-switch / capability banner for the Messages UI."""
    return jsonify(
        {
            "ok": True,
            "messages_execution_enabled": messages_execution_enabled(),
            "send_now_available": messages_execution_enabled(),
            "dry_run_available": True,
            "claude_draft_available": claude_draft_enabled(),
            "banner": (
                None
                if messages_execution_enabled()
                else (
                    "Message sending is currently disabled. "
                    "You can review conversations and validate messages safely."
                )
            ),
        }
    )


@messages_api.route("/accounts", methods=["GET"])
def messages_accounts():
    """List accounts with owner-DM eligibility labels (no Telegram calls)."""
    with get_db_context() as db:
        accounts = db.query(Account).order_by(Account.id.asc()).all()
        rows = []
        for a in accounts:
            elig = evaluate_dm_account_eligibility(db, int(a.id))
            st = getattr(a.status, "value", a.status)
            rows.append(
                {
                    "id": int(a.id),
                    "username": a.username,
                    "first_name": a.first_name,
                    "display_name": (
                        (a.username and f"@{a.username}")
                        or (a.first_name or f"Account #{a.id}")
                    ),
                    "status": st,
                    "purpose": a.purpose,
                    "eligible": elig.eligible,
                    "eligibility_code": elig.code,
                    "eligibility_reason": elig.reason,
                }
            )
    return jsonify({"ok": True, "accounts": rows})


@messages_api.route("/history", methods=["GET"])
def messages_history():
    """Recent private-dialog history via TelegramDmTransport (eligibility gated)."""
    account_id = request.args.get("account_id", type=int)
    peer = (request.args.get("peer") or "").strip()
    limit = request.args.get("limit", 20, type=int)
    if not account_id:
        return jsonify({"ok": False, "error": "VALIDATION_ERROR", "message": "account_id required"}), 400
    if not peer:
        return jsonify({"ok": False, "error": "PEER_INVALID", "message": "peer required"}), 400

    with get_db_context() as db:
        elig = evaluate_dm_account_eligibility(db, int(account_id))
        if not elig.eligible:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": elig.code,
                        "message": elig.reason,
                        "eligibility": elig.to_dict(),
                    }
                ),
                403,
            )

    lim = max(1, min(int(limit or 20), 50))
    transport = TelegramDmTransport()
    try:
        raw = _run_async(transport.fetch_recent_messages_async(int(account_id), peer, lim))
    except Exception as e:
        logger.exception("messages_history_failed", account_id=account_id, error=str(e))
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "HISTORY_UNAVAILABLE",
                    "message": "Unable to load conversation history.",
                    "messages": [],
                }
            ),
            500,
        )

    if not raw.get("ok"):
        # Never surface raw Telethon/asyncio loop affinity text to the owner UI.
        err = str(raw.get("error_code") or "UNKNOWN")
        msg = str(raw.get("error_message") or "")
        if "event loop" in msg.lower() or err == "UNKNOWN":
            msg = "Unable to load conversation history."
            if err == "UNKNOWN":
                err = "HISTORY_UNAVAILABLE"
        return (
            jsonify(
                {
                    "ok": False,
                    "error": err,
                    "message": msg or "Unable to load conversation history.",
                    "messages": [],
                }
            ),
            400,
        )

    messages = []
    for m in raw.get("messages") or []:
        messages.append(
            {
                "message_id": m.get("message_id"),
                "text": m.get("text") or "",
                "timestamp": m.get("timestamp"),
                "is_outgoing": bool(m.get("is_outgoing")),
            }
        )
    return jsonify({"ok": True, "account_id": int(account_id), "peer": peer, "messages": messages})


@messages_api.route("/draft", methods=["POST"])
def messages_draft():
    """Claude draft assistant — returns suggested text only (never sends)."""
    data = request.get_json(silent=True) or {}
    try:
        account_id = int(data.get("account_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "VALIDATION_ERROR", "message": "account_id required"}), 400
    peer = (data.get("peer") or data.get("peer_id") or "").strip()
    instruction = data.get("operator_instruction") or data.get("instruction") or ""
    if not peer:
        return jsonify({"ok": False, "error": "PEER_INVALID", "message": "peer required"}), 400

    svc = ClaudeDraftService()
    with get_db_context() as db:
        result = _run_async(
            svc.draft_reply_async(
                db,
                account_id=account_id,
                peer=peer,
                operator_instruction=str(instruction) if instruction is not None else None,
            )
        )

    if not result.get("ok"):
        code = result.get("error_code") or "CLAUDE_PROVIDER_ERROR"
        status = 503
        if code == "CLAUDE_DISABLED":
            status = 423
        elif code in {"ACCOUNT_INELIGIBLE", "PEER_INVALID", "NO_CONVERSATION_CONTEXT"}:
            status = 400
        elif code == "CLAUDE_NOT_CONFIGURED":
            status = 503
        elif code == "CLAUDE_RATE_LIMITED":
            status = 429
        return jsonify(result), status
    return jsonify(result)


@messages_api.route("/dry-run", methods=["POST"])
def messages_dry_run():
    """Non-mutating validation via OwnerDirectMessageService.dry_run."""
    data = request.get_json(silent=True) or {}
    try:
        account_id = int(data.get("account_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "VALIDATION_ERROR", "message": "account_id required"}), 400
    peer = (data.get("peer") or data.get("peer_id") or "").strip()
    message = data.get("message") if data.get("message") is not None else data.get("text")
    peer_type = (data.get("peer_type") or "private").strip().lower()
    if not peer:
        return jsonify({"ok": False, "error": "PEER_INVALID", "message": "peer required"}), 400
    if message is None:
        return jsonify({"ok": False, "error": "EMPTY_MESSAGE", "message": "message required"}), 400

    svc = OwnerDirectMessageService()
    with get_db_context() as db:
        result = svc.dry_run(
            db,
            account_id=account_id,
            peer_id=peer,
            text=str(message),
            peer_type=peer_type,
        )
    result["ok"] = True
    return jsonify(result)


@messages_api.route("/send-now", methods=["POST"])
def messages_send_now():
    """Live send via OwnerDirectMessageService.send_now (fail-closed when disabled)."""
    data = request.get_json(silent=True) or {}
    try:
        account_id = int(data.get("account_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "VALIDATION_ERROR", "message": "account_id required"}), 400
    peer = (data.get("peer") or data.get("peer_id") or "").strip()
    message = data.get("message") if data.get("message") is not None else data.get("text")
    idempotency_key = (data.get("idempotency_key") or "").strip()
    peer_type = (data.get("peer_type") or "private").strip().lower()
    if not peer:
        return jsonify({"ok": False, "error": "PEER_INVALID", "message": "peer required"}), 400
    if message is None:
        return jsonify({"ok": False, "error": "EMPTY_MESSAGE", "message": "message required"}), 400
    if not idempotency_key:
        return (
            jsonify({"ok": False, "error": "VALIDATION_ERROR", "message": "idempotency_key required"}),
            400,
        )

    svc = OwnerDirectMessageService()
    with get_db_context() as db:
        result = _run_async(
            svc.send_now(
                db,
                account_id=account_id,
                peer_id=peer,
                text=str(message),
                idempotency_key=idempotency_key,
                peer_type=peer_type,
            )
        )
    status_code = 200
    err = result.get("error_code")
    if err == "MESSAGES_DISABLED":
        status_code = 423
    elif err in _ACCOUNT_DENY_CODES:
        status_code = 403
    elif result.get("status") == "FAILED" and not result.get("replay"):
        status_code = 400
    return jsonify(result), status_code


@messages_api.route("/intents", methods=["GET"])
def messages_intents_recent():
    """Recent owner DM intents from owner_dm_intents (no Telegram calls)."""
    limit = request.args.get("limit", 20, type=int)
    lim = max(1, min(int(limit or 20), 50))
    with get_db_context() as db:
        intents = (
            db.query(OwnerDmIntent)
            .order_by(OwnerDmIntent.id.desc())
            .limit(lim)
            .all()
        )
        account_ids = {int(i.account_id) for i in intents}
        labels: dict[int, str] = {}
        if account_ids:
            for a in db.query(Account).filter(Account.id.in_(account_ids)).all():
                labels[int(a.id)] = (
                    (a.username and f"@{a.username}")
                    or (a.first_name or f"Account #{a.id}")
                )
        rows = [
            _intent_owner_safe(
                intent,
                account_label=labels.get(int(intent.account_id)),
                include_ok=False,
            )
            for intent in intents
        ]
    return jsonify({"ok": True, "intents": rows, "limit": lim})


@messages_api.route("/intents/<int:intent_id>", methods=["GET"])
def messages_intent_by_id(intent_id: int):
    with get_db_context() as db:
        intent = db.query(OwnerDmIntent).filter(OwnerDmIntent.id == int(intent_id)).first()
        if not intent:
            return jsonify({"ok": False, "error": "NOT_FOUND", "message": "Intent not found"}), 404
        account = db.query(Account).filter(Account.id == int(intent.account_id)).first()
        label = None
        if account:
            label = (
                (account.username and f"@{account.username}")
                or (account.first_name or f"Account #{account.id}")
            )
        return jsonify(_intent_owner_safe(intent, account_label=label))


@messages_api.route("/intents/by-key/<idempotency_key>", methods=["GET"])
def messages_intent_by_key(idempotency_key: str):
    key = (idempotency_key or "").strip()
    if not key:
        return jsonify({"ok": False, "error": "VALIDATION_ERROR", "message": "key required"}), 400
    with get_db_context() as db:
        intent = (
            db.query(OwnerDmIntent)
            .filter(OwnerDmIntent.idempotency_key == key)
            .first()
        )
        if not intent:
            return jsonify({"ok": False, "error": "NOT_FOUND", "message": "Intent not found"}), 404
        return jsonify(_intent_owner_safe(intent))
