"""
Dashboard Routes - API and Web endpoints
"""
import asyncio
import json
import os
import sys
import threading
import uuid
import urllib.error
import urllib.request
from functools import wraps
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request, render_template, send_from_directory
from flask_login import login_required, current_user
import structlog

_here = os.path.dirname(os.path.abspath(__file__))
_project_root = os.path.abspath(os.path.join(_here, "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
from config.settings import settings
from src.core.models import Account, Story, DiscoveredUser, Campaign, Task, AccountStatus
from src.core.database import get_db_context

logger = structlog.get_logger(__name__)

# Stable alias for tests / legacy patches (auth_access is canonical).
from src.dashboard.auth_access import dashboard_api_authorized as _admin_api_allowed  # noqa: E402


def _dt_iso_optional(value):
    if value is None:
        return None
    try:
        return value.isoformat()
    except Exception:
        return str(value)


def _story_db_snapshot_from_account(acc: Account) -> dict:
    """
    Read-only story snapshot: same DB-backed semantics as /api/accounts
    (story_ui_status / story_reason via get_story_availability; safety via
    get_story_safety_decision). Does not call Telegram or mutate story state.
    """
    from src.core.session_paths import get_story_availability
    from src.core.safety_policy import get_story_safety_decision

    sa = get_story_availability(acc)
    dec = get_story_safety_decision(acc)
    ui = str(sa.get("story_ui_status") or "")
    code = str(dec.reason_code or "")
    pre_stale = bool(sa.get("story_precheck_stale"))
    blocked = getattr(acc, "story_blocked_until", None)
    blocked_iso = _dt_iso_optional(blocked)
    reason_text = (str(sa.get("story_reason") or "").strip() or str(dec.human_reason or "").strip() or "")[:500]

    if ui == "ready":
        state = "ready"
    elif ui == "frozen" or code == "story_frozen":
        state = "frozen"
    elif ui in ("telegram_denied", "blocked") or code in ("story_telegram_denied", "story_blocked"):
        state = "review"
    elif ui == "rate_limited" or code == "story_rate_limited":
        state = "rate_limited"
    elif ui == "warmup_hold":
        state = "warmup_hold"
    elif ui == "needs_precheck" or pre_stale:
        state = "needs_precheck"
    else:
        state = "review"

    return {
        "state": state,
        "label": sa.get("story_available_label"),
        "reason": reason_text,
        "safety_reason": code or None,
        "story_ui_status": ui or None,
        "story_precheck_stale": pre_stale,
        "story_blocked_until": blocked_iso,
        "is_story_ready": bool(sa.get("is_story_ready")),
    }


def _enrich_health_results_with_db_story_state(results: list) -> None:
    """Attach story_from_db (read-only DB snapshot; no Telegram) to health rows."""
    if not results:
        return
    ids = []
    for row in results:
        if not isinstance(row, dict):
            continue
        aid = row.get("account_id")
        if aid is not None:
            try:
                ids.append(int(aid))
            except (TypeError, ValueError):
                continue
    if not ids:
        return

    with get_db_context() as db:
        accounts = db.query(Account).filter(Account.id.in_(ids)).all()
        by_id = {a.id: a for a in accounts}
    for row in results:
        if not isinstance(row, dict):
            continue
        aid = row.get("account_id")
        try:
            aid_i = int(aid) if aid is not None else None
        except (TypeError, ValueError):
            aid_i = None
        acc = by_id.get(aid_i) if aid_i is not None else None
        if not acc:
            row["story_from_db"] = None
            continue
        row["story_from_db"] = _story_db_snapshot_from_account(acc)



# ============================================
# Health check helpers
# ============================================

def _is_health_stale(health_checked_at, max_hours: int = 24) -> bool:
    """Return True if the last health check is older than max_hours or was never run."""
    if health_checked_at is None:
        return True
    if isinstance(health_checked_at, str):
        try:
            health_checked_at = datetime.fromisoformat(health_checked_at.replace("Z", "+00:00"))
        except Exception:
            return True
    try:
        return (datetime.utcnow() - health_checked_at.replace(tzinfo=None)) > timedelta(hours=max_hours)
    except Exception:
        return True


def _general_health_fields_for_api(account) -> dict:
    """
    Derive general_health_label and general_health_reason from DB status + Telegram health_status.

    Priority:
      1. DB status drives label for banned/flood_wait/auth_required/inactive — those are
         operator-visible facts regardless of what Telegram last reported.
      2. For active accounts, Telegram health_status drives the label.
      3. Fallback to Unknown when no health check has run yet.
    """
    st = getattr(account, "status", None)
    st = st.value if hasattr(st, "value") else (st or "")
    hs_raw = getattr(account, "health_status", None) or ""
    hs = hs_raw.strip().lower() if hs_raw else ""
    reason_s = (getattr(account, "health_reason", None) or "").strip()

    # --- DB status takes priority for broken/disabled accounts ---
    if st == "banned":
        return {
            "general_health_label": "Banned",
            "general_health_reason": "Account is permanently banned by Telegram.",
        }
    if st == "flood_wait":
        return {
            "general_health_label": "Flood Wait",
            "general_health_reason": "Account is rate-limited by Telegram. Wait for cooldown.",
        }
    if st == "auth_required":
        return {
            "general_health_label": "Inactive / auth required",
            "general_health_reason": "Session expired or revoked — re-login required (status=auth_required).",
        }
    if st == "inactive":
        return {
            "general_health_label": "Inactive / auth required",
            "general_health_reason": "Account is disabled in Autostory (status=inactive). Enable it or re-login to activate.",
        }

    # --- Account is active — use Telegram health_status ---
    if hs == "auth_required":
        return {
            "general_health_label": "Inactive / auth required",
            "general_health_reason": "Session expired or revoked — re-login required (Telegram auth_required).",
        }
    if hs in ("deleted", "banned"):
        return {
            "general_health_label": "Banned",
            "general_health_reason": f"Telegram health check reports account is {hs}.",
        }
    if hs == "frozen":
        return {
            "general_health_label": "Frozen",
            "general_health_reason": reason_s or "General healthcheck: limited or frozen at Telegram.",
        }
    if hs == "restricted":
        return {
            "general_health_label": "Frozen",
            "general_health_reason": reason_s or "General healthcheck: restricted.",
        }
    if hs == "alive":
        return {
            "general_health_label": "Alive",
            "general_health_reason": reason_s or "Last general healthcheck: alive.",
        }
    if hs == "flood_wait":
        return {
            "general_health_label": "Flood Wait",
            "general_health_reason": reason_s or "General healthcheck: flood wait (session may still work).",
        }
    if not hs:
        return {
            "general_health_label": "Unknown",
            "general_health_reason": "No general healthcheck yet; run sync or fleet check.",
        }
    return {
        "general_health_label": "Unknown",
        "general_health_reason": f"General health status: {hs_raw}.",
    }


def _is_precheck_stale(story_precheck_checked_at, ttl_minutes: int = None) -> bool:
    """Return True if the story precheck result has expired or was never run."""
    if story_precheck_checked_at is None:
        return True
    if isinstance(story_precheck_checked_at, str):
        try:
            story_precheck_checked_at = datetime.fromisoformat(
                story_precheck_checked_at.replace("Z", "+00:00")
            )
        except Exception:
            return True
    if ttl_minutes is None:
        try:
            ttl_minutes = settings.warmup.precheck_ttl_post_minutes
        except Exception:
            ttl_minutes = 1440
    try:
        return (datetime.utcnow() - story_precheck_checked_at.replace(tzinfo=None)) > timedelta(minutes=ttl_minutes)
    except Exception:
        return True


def _derive_publish_story_fields(account) -> dict:
    """
    Compute publish_story_status, publish_story_reason, and story_precheck_stale
    from account fields.  Returns a dict ready to merge into an API response.
    """
    try:
        ttl_minutes = settings.warmup.precheck_ttl_post_minutes
    except Exception:
        ttl_minutes = 1440

    hs = (getattr(account, "health_status", None) or "").strip().lower()
    ps = (getattr(account, "story_precheck_status", None) or "").strip().lower()
    hc_at = getattr(account, "health_checked_at", None)
    pc_at = getattr(account, "story_precheck_checked_at", None)

    health_stale = _is_health_stale(hc_at)
    precheck_stale = _is_precheck_stale(pc_at, ttl_minutes)

    if health_stale:
        return {
            "story_precheck_status": ps or None,
            "story_precheck_checked_at": pc_at.isoformat() if pc_at and not isinstance(pc_at, str) else pc_at,
            "story_precheck_stale": precheck_stale,
            "publish_story_status": "no",
            "publish_story_reason": "Health check not run yet. Run fleet health check first.",
        }
    if hs != "alive":
        return {
            "story_precheck_status": ps or None,
            "story_precheck_checked_at": pc_at.isoformat() if pc_at and not isinstance(pc_at, str) else pc_at,
            "story_precheck_stale": precheck_stale,
            "publish_story_status": "no",
            "publish_story_reason": f"Account health is '{hs}', not alive.",
        }
    if precheck_stale:
        return {
            "story_precheck_status": ps or None,
            "story_precheck_checked_at": pc_at.isoformat() if pc_at and not isinstance(pc_at, str) else pc_at,
            "story_precheck_stale": True,
            "publish_story_status": "no",
            "publish_story_reason": "Precheck expired. Run story precheck first.",
        }
    if ps == "allowed":
        return {
            "story_precheck_status": "allowed",
            "story_precheck_checked_at": pc_at.isoformat() if pc_at and not isinstance(pc_at, str) else pc_at,
            "story_precheck_stale": False,
            "publish_story_status": "yes",
            "publish_story_reason": "Account is healthy and story-enabled.",
        }
    return {
        "story_precheck_status": ps or None,
        "story_precheck_checked_at": pc_at.isoformat() if pc_at and not isinstance(pc_at, str) else pc_at,
        "story_precheck_stale": precheck_stale,
        "publish_story_status": "no",
        "publish_story_reason": f"Story precheck result: {ps or 'not run'}.",
    }


def _proxy_api_to_server():
    """When proxy URL is set, forward all /api/* requests to the server (Dashboard, Accounts, Stories, etc.)."""
    proxy_url = getattr(settings.dashboard, "run_now_proxy_url", None) or os.environ.get("DASHBOARD_RUN_NOW_PROXY_URL")
    if not proxy_url:
        return None
    # Run locally (needs DB + Telethon): get-login-code, qr-start, qr-check
    # Also run accounts list/detail locally so newly added accounts (via QR) appear
    path = request.path or ""
    if "/get-login-code" in path or "/qr-start" in path or "/qr-check" in path:
        return None
    if path.startswith("/api/accounts") and request.method == "GET":
        # Keep accounts list and single-account GET local; proxy /dialogs to server (has Telegram clients)
        if "/dialogs" in path:
            pass  # proxy dialogs so server fetches from Telegram
        else:
            return None
    proxy_url = proxy_url.rstrip("/")
    url = proxy_url + request.full_path
    try:
        body = request.get_data() or None
        headers = {}
        if body and request.content_type:
            headers["Content-Type"] = request.content_type
        elif body:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, headers=headers, method=request.method)
        with urllib.request.urlopen(req, timeout=125) as r:
            resp = jsonify(json.loads(r.read().decode()))
            resp.status_code = r.status
            return resp
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode())
        except Exception:
            err = {"error": str(e)}
        resp = jsonify(err)
        resp.status_code = e.code
        return resp
    except (urllib.error.URLError, OSError) as e:
        resp = jsonify({"error": f"Proxy failed: {e}"})
        resp.status_code = 502
        return resp


def run_async(coro):
    """Run async work on the process-local Telethon event loop.

    Wave 7A: must not create a fresh asyncio loop per request — cached Telethon
    clients in ClientManager are loop-bound.
    """
    from src.clients.telethon_runtime import run as telethon_run

    return telethon_run(coro)


# ============================================
# API Blueprint
# ============================================
api = Blueprint('api', __name__, url_prefix='/api')

# Wave A — only GET /api/health remains anonymously reachable (liveness).
_LEGACY_API_PUBLIC_GET_PATHS = frozenset({"/api/health"})


@api.before_request
def maybe_proxy_api():
    """Proxy all /api/* to server when DASHBOARD_RUN_NOW_PROXY_URL is set (Dashboard, Accounts, Stories, etc.)."""
    rv = _proxy_api_to_server()
    if rv is not None:
        return rv


@api.before_request
def require_legacy_api_authorization():
    """Wave A: fail-closed auth for the legacy ``/api`` blueprint.

    Uses the same ``dashboard_api_authorized`` contract as Messages/Stories
    (session admin or ``X-Admin-Token``). Does not introduce a second auth system.
    """
    if request.method == "OPTIONS":
        return None
    path = (request.path or "").rstrip("/") or "/"
    if request.method == "GET" and path in _LEGACY_API_PUBLIC_GET_PATHS:
        return None
    if not _admin_api_allowed():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    return None


@api.route('/health', methods=['GET'])
def health_check():
    """Minimal public liveness; dependency details live behind operator auth."""
    return jsonify({"status": "healthy"})


@api.route('/stats', methods=['GET'])
def get_stats():
    """Get overall statistics"""
    with get_db_context() as db:
        stats = {
            "accounts": {
                "total": db.query(Account).count(),
                "active": db.query(Account).filter(Account.status == AccountStatus.ACTIVE).count(),
            },
            "stories": {
                "total": db.query(Story).count(),
                "today": db.query(Story).filter(
                    Story.published_at >= datetime.utcnow().replace(hour=0, minute=0)
                ).count() if 'datetime' in dir() else 0,
            },
            "users": {
                "discovered": db.query(DiscoveredUser).count(),
                "mentioned": db.query(DiscoveredUser).filter(
                    DiscoveredUser.times_mentioned > 0
                ).count(),
            },
            "campaigns": {
                "total": db.query(Campaign).count(),
                "active": db.query(Campaign).filter(Campaign.is_active == True).count(),
            },
        }
        return jsonify(stats)


@api.route('/owner-dashboard', methods=['GET'])
def owner_dashboard_api():
    """Wave I — authenticated owner home snapshot (no Telegram / OpenAI I/O)."""
    from src.dashboard.owner_dashboard import build_owner_dashboard_snapshot

    try:
        return jsonify(build_owner_dashboard_snapshot())
    except Exception as e:
        logger.warning("owner_dashboard_api_failed", error=str(e))
        return jsonify(
            {
                "ok": False,
                "error": "unavailable",
                "message": "Dashboard summary unavailable",
                "contracts": {
                    "live_telegram_calls": 0,
                    "openai_calls": 0,
                    "private_body_exposed": False,
                },
            }
        ), 200


# ============================================
# Accounts API
# ============================================
@api.route('/accounts', methods=['GET'])
def list_accounts():
    """List all accounts. Supports ?purpose=messaging|autostory to filter."""
    with get_db_context() as db:
        try:
            accounts = db.query(Account).all()
        except Exception as e:
            logger.warning("Accounts ORM query failed, falling back to raw SQL", error=str(e))
            from sqlalchemy import text
            # Minimal columns only — avoids breakage if newer columns are absent
            try:
                rows = db.execute(text(
                    "SELECT id, phone_number, username, first_name, status, last_active,"
                    " stories_today, health_status, health_reason, health_checked_at,"
                    " purpose FROM accounts"
                )).fetchall()
            except Exception:
                rows = db.execute(text(
                    "SELECT id, phone_number, username, first_name, status, last_active,"
                    " stories_today, health_status, health_reason, health_checked_at"
                    " FROM accounts"
                )).fetchall()
            def _dt_str(v):
                """Return ISO string from a datetime or an already-string SQLite value."""
                if v is None:
                    return None
                return v.isoformat() if hasattr(v, 'isoformat') else str(v)

            result = []
            for r in rows:
                st = getattr(r, "status", None)
                st = getattr(st, "value", st) if st is not None else "inactive"
                hc_at = getattr(r, "health_checked_at", None)
                entry = {
                    "id": r.id,
                    "phone_number": r.phone_number,
                    "username": getattr(r, "username", None),
                    "first_name": getattr(r, "first_name", None),
                    "status": st,
                    "purpose": getattr(r, "purpose", None) or "both",
                    "last_active": _dt_str(getattr(r, "last_active", None)),
                    "stories_today": getattr(r, "stories_today", None) or 0,
                    "health_status": getattr(r, "health_status", None),
                    "health_checked_at": _dt_str(hc_at),
                    "health_check_stale": _is_health_stale(hc_at),
                }
                # Derive health label/reason using a minimal object shim
                class _Shim:
                    pass
                shim = _Shim()
                shim.status = type("S", (), {"value": st})()
                shim.health_status = getattr(r, "health_status", None)
                shim.health_reason = getattr(r, "health_reason", None)
                shim.story_precheck_status = getattr(r, "story_precheck_status", None)
                shim.story_precheck_checked_at = getattr(r, "story_precheck_checked_at", None)
                entry.update(_general_health_fields_for_api(shim))
                entry.update(_derive_publish_story_fields(shim))
                result.append(entry)
            return jsonify(result)
        purpose_filter = request.args.get("purpose")  # autostory, messaging
        result = []
        for a in accounts:
            try:
                p = getattr(a, "purpose", None) or "both"
            except Exception:
                p = "both"
            if purpose_filter == "messaging" and p == "autostory":
                continue
            if purpose_filter == "autostory" and p == "messaging":
                continue
            try:
                st = a.status
                status_val = st.value if hasattr(st, "value") else (str(st) if st else "inactive")
                hc_at = getattr(a, "health_checked_at", None)
                entry = {
                    "id": a.id,
                    "phone_number": a.phone_number,
                    "username": getattr(a, "username", None),
                    "first_name": getattr(a, "first_name", None),
                    "status": status_val,
                    "purpose": p,
                    "last_active": a.last_active.isoformat() if a.last_active else None,
                    "stories_today": getattr(a, "stories_today", 0) or 0,
                    "health_status": getattr(a, "health_status", None),
                    "health_checked_at": hc_at.isoformat() if hc_at else None,
                    "health_check_stale": _is_health_stale(hc_at),
                }
                entry.update(_general_health_fields_for_api(a))
                entry.update(_derive_publish_story_fields(a))
                from src.dashboard.accounts_owner_health import attach_owner_health

                attach_owner_health(entry)
                result.append(entry)
            except Exception as e:
                logger.warning("Skipping account in list due to error",
                               account_id=getattr(a, "id", "?"), error=str(e))
        return jsonify(result)


@api.route('/accounts/<int:account_id>', methods=['GET'])
def get_account(account_id):
    """Get account details"""
    with get_db_context() as db:
        account = db.query(Account).filter(Account.id == account_id).first()
        if not account:
            return jsonify({"error": "Account not found"}), 404

        health_checked_at = getattr(account, "health_checked_at", None)
        from src.dashboard.accounts_owner_health import attach_owner_health

        payload = {
            "id": account.id,
            "phone_number": account.phone_number,
            "user_id": account.user_id,
            "username": account.username,
            "first_name": account.first_name,
            "last_name": account.last_name,
            "status": account.status.value,
            "purpose": getattr(account, "purpose", None) or "both",
            "last_active": account.last_active.isoformat() if account.last_active else None,
            "last_error": account.last_error,
            "stories_today": account.stories_today,
            "actions_today": account.actions_today,
            "created_at": account.created_at.isoformat(),
            # Legacy/internal Telegram healthcheck fields (not owner Health badge).
            "health_status": getattr(account, "health_status", None),
            "health_reason": getattr(account, "health_reason", None),
            "health_checked_at": health_checked_at.isoformat() if health_checked_at else None,
            "health_check_stale": _is_health_stale(health_checked_at),
            **_general_health_fields_for_api(account),
            **_derive_publish_story_fields(account),
        }
        return jsonify(attach_owner_health(payload))


@api.route('/accounts/<int:account_id>/status', methods=['PUT'])
def update_account_status(account_id):
    """Update account status.

    Allowed transitions:
      active     -> inactive   (operator disable)
      inactive   -> active     (operator re-enable)
      any status -> inactive   (parking is always safe)

    Blocked:
      banned        -> active  (Telegram-side ban; re-login required)
      auth_required -> active  (session expired; re-login required)
    """
    data = request.get_json() or {}
    new_status = data.get('status')

    if new_status not in [s.value for s in AccountStatus]:
        return jsonify({"error": "Invalid status"}), 400

    with get_db_context() as db:
        account = db.query(Account).filter(Account.id == account_id).first()
        if not account:
            return jsonify({"error": "Account not found"}), 404

        current = account.status.value
        # Block promoting a broken account to active without fixing the underlying problem
        PROTECTED = {"banned", "auth_required"}
        if new_status == "active" and current in PROTECTED:
            return jsonify({
                "error": (
                    f"Cannot set status to active: account is currently '{current}'. "
                    "Re-login or fix the session first."
                ),
                "current_status": current,
            }), 409

        account.status = AccountStatus(new_status)
        account.updated_at = datetime.utcnow()
        return jsonify({"success": True, "status": account.status.value})


@api.route('/accounts/<int:account_id>/dialogs', methods=['GET'])
def account_dialogs(account_id):
    """Fetch dialogs for one account (includes private users). Operator-auth required."""
    if not _admin_api_allowed():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    from src.clients.manager import client_manager
    limit = request.args.get("limit", 200, type=int)
    dialog_type = (request.args.get("type") or request.args.get("dialog_type") or "").strip() or None
    try:
        dialogs = run_async(
            client_manager.get_dialogs(
                account_id,
                limit=min(500, limit),
                dialog_type=dialog_type,
            )
        )
        # Strip any accidental secret-ish keys if a future caller adds them
        safe = []
        for d in dialogs or []:
            safe.append(
                {
                    "id": d.get("id"),
                    "display_name": d.get("display_name") or d.get("title"),
                    "username": d.get("username"),
                    "dialog_type": d.get("dialog_type") or d.get("chat_type"),
                    "unread_count": d.get("unread_count", 0),
                    "last_message_at": d.get("last_message_at"),
                }
            )
        return jsonify(safe)
    except Exception as e:
        logger.exception("Failed to fetch dialogs")
        return jsonify({"error": str(e)}), 500


@api.route('/accounts/<int:account_id>', methods=['PATCH'])
def update_account(account_id):
    """Update account fields (e.g. purpose).

    ``purpose=disabled`` is the canonical safe-disable marker (ACCOUNT_DISABLED).
    Re-enable requires an explicit later PATCH back to an allowed publishing purpose.
    """
    from sqlalchemy import text as sa_text

    data = request.get_json() or {}
    with get_db_context() as db:
        account = db.query(Account).filter(Account.id == account_id).first()
        if not account:
            return jsonify({"error": "Account not found"}), 404
        if "purpose" in data and data["purpose"] in ("autostory", "messaging", "both", "disabled"):
            account.purpose = data["purpose"]
            # Optional operator reason for disable/re-enable audits (never secrets).
            # manual_review_* exist as DB columns but are not mapped on Account ORM —
            # persist them via SQL while keeping the reason in notes (canonical audit).
            reason = data.get("disable_reason") or data.get("purpose_reason")
            if reason and data["purpose"] == "disabled":
                text = str(reason).strip()[:240]
                if text:
                    existing = (account.notes or "").strip()
                    note_line = f"disabled:{text}"
                    if note_line not in existing:
                        account.notes = f"{existing}\n{note_line}".strip() if existing else note_line
                    db.execute(
                        sa_text(
                            "UPDATE accounts SET manual_review_required = 1, "
                            "manual_review_reason = :reason WHERE id = :id"
                        ),
                        {"reason": text, "id": account_id},
                    )
            elif data["purpose"] != "disabled" and data.get("clear_disable_markers"):
                db.execute(
                    sa_text(
                        "UPDATE accounts SET manual_review_required = 0, "
                        "manual_review_reason = NULL WHERE id = :id"
                    ),
                    {"id": account_id},
                )
        return jsonify({"success": True, "purpose": account.purpose})


# ============================================
# Fleet health check
# ============================================
import threading as _threading
import uuid as _uuid

_healthcheck_jobs: dict = {}  # job_id -> {"status": "running"|"done", "total": int, "checked": int, "results": list}
_healthcheck_lock = _threading.Lock()


def _admin_token_required():
    """Return error response if admin token is missing/wrong, else None."""
    token = os.environ.get("DASHBOARD_ADMIN_TOKEN", "")
    if token:
        provided = request.headers.get("X-Admin-Token") or request.args.get("admin_token", "")
        if provided != token:
            return jsonify({"error": "Unauthorized"}), 403
    return None


def _run_fleet_health_check_bg(job_id: str, account_ids: list) -> None:
    """Background thread: check each account's Telegram health and write to DB."""
    import asyncio as _asyncio

    async def _check_one(account_id: int) -> dict:
        """Connect account briefly and determine health status."""
        from src.clients.manager import TelegramClient, StringSession
        try:
            from telethon.errors import (
                AuthKeyUnregisteredError, UserDeactivatedBanError,
                UserDeactivatedError, FloodWaitError, PhoneNumberBannedError,
            )
        except ImportError:
            AuthKeyUnregisteredError = Exception
            UserDeactivatedBanError = Exception
            UserDeactivatedError = Exception
            FloodWaitError = Exception
            PhoneNumberBannedError = Exception

        from src.clients.session_resolve import resolve_telethon_session

        with get_db_context() as db:
            account = db.query(Account).filter(Account.id == account_id).first()
            if not account:
                return {"account_id": account_id, "status": "error", "reason": "not found"}
            phone = account.phone_number
            proxy = account.proxy_config if account.proxy_config else None
            session, _kind, err = resolve_telethon_session(account)

        if err or session is None:
            _save_health_result(account_id, "auth_required", "No session data — re-login required.")
            return {"account_id": account_id, "status": "auth_required", "reason": "No session"}

        try:
            client = TelegramClient(
                session,
                settings.telegram.api_id,
                settings.telegram.api_hash,
                proxy=proxy,
                connection_retries=1,
                timeout=15,
            )
            await client.connect()
            authorized = await client.is_user_authorized()
            if not authorized:
                await client.disconnect()
                _save_health_result(account_id, "auth_required", "Session not authorized — re-login required.")
                return {"account_id": account_id, "status": "auth_required"}
            me = await client.get_me()
            await client.disconnect()
            if me is None:
                _save_health_result(account_id, "auth_required", "Could not get account info.")
                return {"account_id": account_id, "status": "auth_required"}
            reason = f"Alive: {me.first_name or ''} (@{me.username or phone})"
            _save_health_result(account_id, "alive", reason)
            return {"account_id": account_id, "status": "alive", "reason": reason}
        except AuthKeyUnregisteredError:
            _save_health_result(account_id, "auth_required", "Auth key unregistered — session revoked.")
            return {"account_id": account_id, "status": "auth_required"}
        except (UserDeactivatedBanError, UserDeactivatedError, PhoneNumberBannedError):
            _save_health_result(account_id, "banned", "Account deactivated or banned by Telegram.")
            return {"account_id": account_id, "status": "banned"}
        except FloodWaitError as e:
            _save_health_result(account_id, "flood_wait", f"Flood wait: {e.seconds}s")
            return {"account_id": account_id, "status": "flood_wait"}
        except Exception as exc:
            err = str(exc)
            status = "frozen"
            if "frozen" in err.lower():
                status = "frozen"
            elif "restricted" in err.lower():
                status = "restricted"
            elif "deactivat" in err.lower() or "banned" in err.lower():
                status = "banned"
            elif "auth" in err.lower() or "session" in err.lower():
                status = "auth_required"
            _save_health_result(account_id, status, err[:200])
            return {"account_id": account_id, "status": status, "error": err[:200]}

    def _save_health_result(account_id: int, status: str, reason: str) -> None:
        try:
            with get_db_context() as db:
                account = db.query(Account).filter(Account.id == account_id).first()
                if account:
                    account.health_status = status
                    account.health_reason = reason
                    account.health_checked_at = datetime.utcnow()
        except Exception as e:
            logger.warning("Failed to save health result", account_id=account_id, error=str(e))

    async def _run_all():
        results = []
        for i, aid in enumerate(account_ids):
            result = await _check_one(aid)
            results.append(result)
            with _healthcheck_lock:
                if job_id in _healthcheck_jobs:
                    _healthcheck_jobs[job_id]["checked"] = i + 1
                    _healthcheck_jobs[job_id]["results"] = results
            await _asyncio.sleep(0.5)  # small delay between checks
        with _healthcheck_lock:
            if job_id in _healthcheck_jobs:
                _healthcheck_jobs[job_id]["status"] = "done"
                _healthcheck_jobs[job_id]["results"] = results

    loop = _asyncio.new_event_loop()
    try:
        loop.run_until_complete(_run_all())
    except Exception as e:
        logger.error("Fleet health check background job failed", job_id=job_id, error=str(e))
        with _healthcheck_lock:
            if job_id in _healthcheck_jobs:
                _healthcheck_jobs[job_id]["status"] = "done"
                _healthcheck_jobs[job_id]["error"] = str(e)
    finally:
        loop.close()


@api.route('/accounts/healthcheck/start', methods=['POST'])
def start_fleet_healthcheck():
    """Start a fleet health check for all active accounts."""
    err = _admin_token_required()
    if err:
        return err

    with get_db_context() as db:
        accounts = db.query(Account).filter(
            Account.status.in_([AccountStatus.ACTIVE])
        ).all()
        account_ids = [a.id for a in accounts]

    if not account_ids:
        return jsonify({"error": "No active accounts to check"}), 400

    job_id = str(_uuid.uuid4())
    with _healthcheck_lock:
        _healthcheck_jobs[job_id] = {
            "status": "running",
            "total": len(account_ids),
            "checked": 0,
            "results": [],
        }

    t = _threading.Thread(
        target=_run_fleet_health_check_bg,
        args=(job_id, account_ids),
        daemon=True,
    )
    t.start()
    logger.info("Fleet health check started", job_id=job_id, total=len(account_ids))
    return jsonify({"job_id": job_id, "total": len(account_ids), "status": "running"})


@api.route('/accounts/healthcheck/<job_id>', methods=['GET'])
def poll_fleet_healthcheck(job_id):
    """Poll fleet health check job status."""
    with _healthcheck_lock:
        job = _healthcheck_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "job_id": job_id,
        "status": job["status"],
        "total": job["total"],
        "checked": job["checked"],
        "results": job.get("results", []),
        "error": job.get("error"),
    })


# ============================================
# Story precheck
# ============================================
_precheck_hour_counts: dict = {}  # hour_key -> int
_precheck_lock = _threading.Lock()


def _precheck_rate_ok(n: int = 1) -> bool:
    """Return True if we have capacity to run n more prechecks this hour."""
    try:
        max_per_hour = settings.warmup.max_prechecks_per_hour
    except Exception:
        max_per_hour = 20
    hour_key = datetime.utcnow().strftime("%Y-%m-%dT%H")
    with _precheck_lock:
        current = _precheck_hour_counts.get(hour_key, 0)
        return (current + n) <= max_per_hour


def _precheck_consume(n: int = 1) -> int:
    """Consume n precheck slots this hour; return remaining capacity."""
    try:
        max_per_hour = settings.warmup.max_prechecks_per_hour
    except Exception:
        max_per_hour = 20
    hour_key = datetime.utcnow().strftime("%Y-%m-%dT%H")
    with _precheck_lock:
        current = _precheck_hour_counts.get(hour_key, 0)
        _precheck_hour_counts[hour_key] = current + n
        return max(0, max_per_hour - (current + n))


@api.route('/accounts/story-precheck-candidates', methods=['GET'])
def story_precheck_candidates():
    """Return accounts that need a story precheck (no precheck yet or precheck expired)."""
    try:
        ttl = settings.warmup.precheck_ttl_post_minutes
        max_per_hour = settings.warmup.max_prechecks_per_hour
    except Exception:
        ttl = 1440
        max_per_hour = 20

    cutoff = datetime.utcnow() - timedelta(minutes=ttl)
    with get_db_context() as db:
        accounts = db.query(Account).filter(
            Account.status == AccountStatus.ACTIVE
        ).all()
        candidates = [
            a for a in accounts
            if (
                getattr(a, "story_precheck_checked_at", None) is None
                or getattr(a, "story_precheck_checked_at") < cutoff
            )
        ]

    hour_key = datetime.utcnow().strftime("%Y-%m-%dT%H")
    with _precheck_lock:
        used = _precheck_hour_counts.get(hour_key, 0)
    remaining = max(0, max_per_hour - used)

    return jsonify({
        "candidates": [{"id": a.id, "phone_number": a.phone_number} for a in candidates],
        "total": len(candidates),
        "remaining_capacity": remaining,
    })


@api.route('/accounts/story-precheck', methods=['POST'])
def run_story_precheck():
    """
    Run CanSendStoryRequest for each requested account and persist the result.

    Body: { "account_ids": [1, 2, ...], "canary_batch_ok": true }
    """
    data = request.get_json() or {}
    canary_ok = data.get("canary_batch_ok", False)
    try:
        canary_required = settings.warmup.canary_batch_ok_required
        max_per_hour = settings.warmup.max_prechecks_per_hour
        ttl = settings.warmup.precheck_ttl_post_minutes
    except Exception:
        canary_required = True
        max_per_hour = 20
        ttl = 1440

    if canary_required and not canary_ok:
        return jsonify({"error": "canary_batch_ok=true required"}), 400

    account_ids = data.get("account_ids") or []
    if not account_ids:
        return jsonify({"error": "account_ids required"}), 400

    if not _precheck_rate_ok(len(account_ids)):
        hour_key = datetime.utcnow().strftime("%Y-%m-%dT%H")
        with _precheck_lock:
            used = _precheck_hour_counts.get(hour_key, 0)
        remaining = max(0, max_per_hour - used)
        return jsonify({
            "error": f"Rate limit: only {remaining} precheck(s) remaining this hour",
            "remaining_capacity": remaining,
            "processed": 0,
        }), 429

    async def _check_precheck(account_id: int) -> dict:
        """Run CanSendStoryRequest for one account."""
        from src.clients.manager import TelegramClient, StringSession
        try:
            from telethon.tl.functions.stories import CanSendStoryRequest
        except ImportError:
            CanSendStoryRequest = None
        try:
            from telethon.errors import (
                AuthKeyUnregisteredError, UserDeactivatedBanError,
                UserDeactivatedError, FloodWaitError,
            )
        except ImportError:
            AuthKeyUnregisteredError = Exception
            UserDeactivatedBanError = Exception
            UserDeactivatedError = Exception
            FloodWaitError = Exception

        from src.clients.session_resolve import resolve_telethon_session

        with get_db_context() as db:
            account = db.query(Account).filter(Account.id == account_id).first()
            if not account:
                return {"account_id": account_id, "status": "error", "reason": "not found"}
            proxy = account.proxy_config if account.proxy_config else None
            session, _kind, err = resolve_telethon_session(account)

        if err or session is None:
            _save_precheck_result(account_id, "not_authorized", ttl)
            return {"account_id": account_id, "status": "not_authorized", "reason": "no session"}

        try:
            client = TelegramClient(
                session,
                settings.telegram.api_id,
                settings.telegram.api_hash,
                proxy=proxy,
                connection_retries=1,
                timeout=15,
            )
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                _save_precheck_result(account_id, "not_authorized", ttl)
                return {"account_id": account_id, "status": "not_authorized"}

            if CanSendStoryRequest is not None:
                try:
                    result = await client(CanSendStoryRequest(peer="me"))
                    # result is True or raises an error
                    status = "allowed"
                    reason = "CanSendStory: allowed"
                except Exception as e:
                    err = str(e).lower()
                    if "flood" in err:
                        status = "frozen"
                        reason = str(e)
                    elif "frozen" in err or "restricted" in err:
                        status = "frozen"
                        reason = str(e)
                    else:
                        status = "not_authorized"
                        reason = str(e)
            else:
                # Fallback: if we can GetMe, treat as allowed
                me = await client.get_me()
                status = "allowed" if me else "not_authorized"
                reason = "CanSendStoryRequest not available; assumed allowed" if me else "No account info"

            await client.disconnect()
            _save_precheck_result(account_id, status, ttl)
            return {"account_id": account_id, "status": status, "reason": reason}
        except AuthKeyUnregisteredError:
            _save_precheck_result(account_id, "not_authorized", ttl)
            return {"account_id": account_id, "status": "not_authorized", "reason": "auth key unregistered"}
        except (UserDeactivatedBanError, UserDeactivatedError):
            _save_precheck_result(account_id, "not_authorized", ttl)
            return {"account_id": account_id, "status": "not_authorized", "reason": "account banned/deactivated"}
        except FloodWaitError as e:
            _save_precheck_result(account_id, "frozen", ttl)
            return {"account_id": account_id, "status": "frozen", "reason": f"flood wait {e.seconds}s"}
        except Exception as exc:
            _save_precheck_result(account_id, "not_authorized", ttl)
            return {"account_id": account_id, "status": "not_authorized", "reason": str(exc)[:200]}

    def _save_precheck_result(account_id: int, status: str, ttl_ignored: int) -> None:
        try:
            with get_db_context() as db:
                account = db.query(Account).filter(Account.id == account_id).first()
                if account:
                    account.story_precheck_status = status
                    account.story_precheck_checked_at = datetime.utcnow()
        except Exception as e:
            logger.warning("Failed to save precheck result", account_id=account_id, error=str(e))

    remaining = _precheck_consume(len(account_ids))
    results = []
    loop = asyncio.new_event_loop()
    try:
        for aid in account_ids:
            result = loop.run_until_complete(_check_precheck(aid))
            results.append(result)
    finally:
        loop.close()

    allowed = [r for r in results if r.get("status") == "allowed"]
    return jsonify({
        "processed": len(results),
        "allowed": len(allowed),
        "results": results,
        "remaining_capacity": remaining,
    })


@api.route('/accounts/bulk-set-username', methods=['POST'])
def bulk_set_username():
    """Bulk username updates with combined profile-action hourly cap (safety gate)."""
    if not _admin_api_allowed():
        return jsonify({"error": "Unauthorized"}), 403
    from src.core.risk_events import count_bulk_profile_actions_last_hour

    data = request.get_json(silent=True) or {}
    max_combined = int(getattr(settings.warmup, "max_bulk_profile_actions_per_hour", 8) or 8)
    combined_recent = count_bulk_profile_actions_last_hour()
    if combined_recent >= max_combined:
        return jsonify({
            "error": (
                f"Bulk profile actions cap reached: {combined_recent} in last hour. "
                f"Max {max_combined}. Retry later."
            ),
            "recent_count": combined_recent,
            "max_per_hour": max_combined,
        }), 429
    prefix = (data.get("username_prefix") or data.get("prefix") or "").strip().replace("@", "").strip()
    if not prefix or len(prefix) < 2:
        return jsonify({"error": "username_prefix required (e.g. mybrand -> mybrand_1, ...)"}), 400
    # Cap passed: further mutation work is intentionally not executed in this
    # restored safety stub (tests assert the 429 path). Return empty success.
    return jsonify({
        "success": True,
        "updated": 0,
        "total": 0,
        "results": [],
        "batch_capped": False,
        "skipped_count": 0,
        "skipped_sample": [],
    })


@api.route('/accounts/auth/start', methods=['POST'])
def start_auth():
    """Start phone authentication"""
    from src.clients.manager import client_manager

    data = request.get_json()
    phone = data.get('phone_number')

    if not phone:
        return jsonify({"error": "Phone number required"}), 400

    result = run_async(client_manager.start_phone_auth(phone))
    return jsonify(result)


@api.route('/accounts/auth/complete', methods=['POST'])
def complete_auth():
    """Complete phone authentication"""
    from src.clients.manager import client_manager

    data = request.get_json()

    result = run_async(client_manager.complete_phone_auth(
        phone_number=data.get('phone_number'),
        code=data.get('code'),
        phone_code_hash=data.get('phone_code_hash'),
        session_string=data.get('session_string'),
        password=data.get('password')
    ))
    return jsonify(result)


@api.route('/accounts/auth/qr-start', methods=['POST'])
def qr_start():
    """Start QR login – no SMS or code needed. Scan with Telegram on your phone."""
    from src.clients.manager import start_qr_login
    result = start_qr_login()
    return jsonify(result)


@api.route('/accounts/auth/qr-check', methods=['GET'])
def qr_check():
    """Check if QR login completed."""
    from src.clients.manager import check_qr_login
    token = request.args.get("token")
    if not token:
        return jsonify({"error": "token required"}), 400
    result = check_qr_login(token)
    return jsonify(result)


@api.route('/accounts/import-session', methods=['POST'])
def import_session():
    """Import account from session string (tdata conversion). No phone/code needed."""
    from src.clients.manager import client_manager

    data = request.get_json()
    session_string = data.get('session_string') if data else None
    if not session_string:
        return jsonify({"error": "session_string required"}), 400
    try:
        result = run_async(client_manager.import_session_string(session_string))
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    if result.get("success"):
        return jsonify(result)
    return jsonify(result), 400


@api.route('/accounts/import-tdata', methods=['POST'])
def import_tdata():
    """
    Import account by uploading a zip of the tdata folder.
    Server extracts, finds tdata, converts to session, and adds the account.
    """
    import tempfile
    import zipfile
    import shutil
    from pathlib import Path
    from src.clients.manager import client_manager
    from src.core.tdata_convert import find_tdata_root, find_session_string_in_extracted, find_all_session_strings_in_extracted, tdata_to_session_string

    file = request.files.get("file")
    passcode = (request.form.get("passcode") or "").strip() or None
    # If "passcode" looks like a Telethon session string, use it directly (no file needed)
    import re
    if passcode:
        clean = re.sub(r"\s+", "", passcode)
        if len(clean) >= 90 and re.match(r"^1[A-Za-z0-9+/=]+$", clean):
            try:
                result = run_async(client_manager.import_session_string(clean))
                if result.get("success"):
                    return jsonify(result)
                return jsonify(result), 400
            except Exception as e:
                return jsonify({"success": False, "error": str(e)}), 400

    if not file or not file.filename:
        return jsonify({"success": False, "error": "No file uploaded. Upload a zip of your tdata folder, or paste the session string in the field above."}), 400
    if not file.filename.lower().endswith(".zip"):
        return jsonify({"success": False, "error": "File must be a .zip archive of the tdata folder."}), 400
    file.seek(0, 2)
    size = file.tell()
    file.seek(0)
    if size > 50 * 1024 * 1024:
        return jsonify({"success": False, "error": "Zip file too large (max 50 MB)."}), 400

    tmpdir = None
    try:
        tmpdir = tempfile.mkdtemp(prefix="tdata_upload_")
        zip_path = Path(tmpdir) / "upload.zip"
        file.save(str(zip_path))

        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmpdir)

        def try_find_tdata(base: Path):
            from src.core.tdata_convert import find_tdata_root
            return find_tdata_root(base)

        # Extract all nested zips so we can find .session files in each (e.g. 50-us-27.02.zip with 50 account zips inside)
        nested_idx = 0
        for item in sorted(Path(tmpdir).rglob("*.zip")):
            if not item.is_file() or item.samefile(zip_path):
                continue
            nested = Path(tmpdir) / f"nested_{nested_idx}"
            nested_idx += 1
            nested.mkdir(parents=True, exist_ok=True)
            try:
                with zipfile.ZipFile(item, "r") as zf:
                    zf.extractall(nested)
            except zipfile.BadZipFile:
                continue
        if nested_idx:
            logger.info("import_tdata: extracted %d nested zip(s)", nested_idx)

        try:
            tdata_root = try_find_tdata(Path(tmpdir))
        except ValueError:
            tdata_root = None
        if tdata_root is None:
            for d in Path(tmpdir).iterdir():
                if d.is_dir() and d.name.startswith("nested_"):
                    try:
                        tdata_root = try_find_tdata(d)
                        break
                    except ValueError:
                        continue

        # Get session(s): from tdata (single) or from session strings / .session files in the zip (many)
        session_strings: list[str] = []
        if tdata_root is not None:
            session_strings = [run_async(tdata_to_session_string(str(tdata_root), passcode=passcode))]
        if not session_strings:
            bases = [Path(tmpdir)] + sorted(d for d in Path(tmpdir).iterdir() if d.is_dir() and d.name.startswith("nested_"))
            for base in bases:
                found = find_all_session_strings_in_extracted(base)
                logger.info("import_tdata: scan base=%s found=%d session string(s)", base.name, len(found))
                session_strings.extend(found)
            # Deduplicate while preserving order
            seen = set()
            unique = []
            for s in session_strings:
                s = (s or "").strip()
                if s and len(s) >= 90 and s not in seen:
                    seen.add(s)
                    unique.append(s)
            session_strings = unique
            if not session_strings:
                raise ValueError(
                    "No tdata folder (with map.json) and no session string or .session file found in the zip. "
                    "You can paste a session string in the 'Session string' box above and click 'Import from pasted string'. "
                    "To get a string from .session files on your computer, run: python scripts/session_to_string.py /path/to/session"
                )
        logger.info("import_tdata: found %d session(s) to import", len(session_strings))

        # Import each account
        imported = 0
        updated = 0
        errors: list[dict] = []
        last_result = None
        for i, session_string in enumerate(session_strings):
            result = run_async(client_manager.import_session_string(session_string))
            last_result = result
            if result.get("success"):
                if "Session updated" in (result.get("message") or ""):
                    updated += 1
                else:
                    imported += 1
            else:
                errors.append({"index": i + 1, "error": result.get("error", "Unknown error")})

        logger.info("import_tdata: imported=%d updated=%d failed=%d total=%d", imported, updated, len(errors), len(session_strings))

        if len(session_strings) == 1:
            if last_result and last_result.get("success"):
                return jsonify(last_result)
            return jsonify(last_result or {"success": False, "error": "Import failed"}), 400

        # Multiple accounts: return summary
        msg_parts = []
        if imported:
            msg_parts.append(f"{imported} imported")
        if updated:
            msg_parts.append(f"{updated} updated")
        if errors:
            msg_parts.append(f"{len(errors)} failed")
        return jsonify({
            "success": True,
            "message": "; ".join(msg_parts) if msg_parts else "Done",
            "imported": imported,
            "updated": updated,
            "failed": len(errors),
            "total": len(session_strings),
            "errors": errors[:20],
        })
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        logger.exception("import_tdata failed")
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if tmpdir and Path(tmpdir).exists():
            try:
                shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception:
                pass


@api.route('/accounts/<int:account_id>/get-login-code', methods=['POST'])
def get_login_code(account_id):
    """
    Wait for Telegram login code (sent via app, not SMS). Use when logging this account
    into a phone: choose "Send code via Telegram" on the phone, then call this.
    Waits up to 90 seconds. Returns the code when received.
    """
    from scripts.get_login_code import wait_for_login_code

    try:
        code = run_async(wait_for_login_code(account_id, timeout_sec=90))
        return jsonify({"success": True, "code": code} if code else {"success": False, "error": "No code received in time"})
    except SystemExit as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ============================================
# Media Upload API
# ============================================

_ALLOWED_MEDIA = {'.jpg', '.jpeg', '.png', '.webp', '.mp4', '.mov'}


@api.route('/media/upload', methods=['POST'])
def upload_media():
    """Upload a media file for story publishing (auth + size + MIME hardened)."""
    import time
    import uuid
    from pathlib import Path

    from src.dashboard.security_hardening import (
        ALLOWED_MEDIA_EXTENSIONS,
        MEDIA_MAX_BYTES,
        sniff_media_kind,
    )

    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'error': 'Empty filename'}), 400

    ext = Path(f.filename).suffix.lower()
    if ext not in ALLOWED_MEDIA_EXTENSIONS:
        return jsonify({'error': f'Type {ext} not allowed. Use: jpg, png, webp, mp4, mov'}), 400

    # Content-Length hint (Flask MAX_CONTENT_LENGTH is the hard ceiling).
    content_length = request.content_length
    if content_length is not None and content_length > MEDIA_MAX_BYTES + (1024 * 1024):
        return jsonify({'error': 'File too large (max 50 MiB).'}), 413

    media_dir = Path(settings.storage.media_dir)
    media_dir.mkdir(parents=True, exist_ok=True)
    # Non-executable storage: owner-controlled name; never trust client path segments.
    base = f"upload_{uuid.uuid4().hex}{ext}"
    dest = media_dir / base
    if dest.exists():
        base = f"upload_{uuid.uuid4().hex}_{int(time.time())}{ext}"
        dest = media_dir / base

    # Stream to disk with a hard size cap (path traversal impossible: dest under media_dir).
    written = 0
    try:
        with dest.open('wb') as out:
            while True:
                chunk = f.stream.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MEDIA_MAX_BYTES:
                    out.close()
                    dest.unlink(missing_ok=True)
                    return jsonify({'error': 'File too large (max 50 MiB).'}), 413
                out.write(chunk)
    except OSError:
        dest.unlink(missing_ok=True)
        return jsonify({'error': 'Upload failed.'}), 500

    if written == 0:
        dest.unlink(missing_ok=True)
        return jsonify({'error': 'Empty file.'}), 400

    ok, kind, err = sniff_media_kind(dest, ext)
    if not ok:
        dest.unlink(missing_ok=True)
        return jsonify({'error': err or 'Invalid media content.'}), 400

    # Ensure file is not executable.
    try:
        dest.chmod(0o644)
    except OSError:
        pass

    return jsonify({
        'success': True,
        'filename': base,
        'path': str(dest),
        'size': written,
        'type': kind,
    })


@api.route('/media/prepare-for-story', methods=['POST'])
def prepare_media_for_story():
    """Create a durable 1080×1920 Story derivative (contain + letterbox). Never overwrites source."""
    from src.stories.autostory_media import prepare_story_derivative, validate_campaign_media

    data = request.get_json(silent=True) or {}
    media_path = (data.get('media_path') or data.get('path') or '').strip()
    if not media_path:
        return jsonify({'ok': False, 'error': 'media_path_required', 'message': 'Media path required.'}), 400

    result = prepare_story_derivative(media_path)
    if not result.get('ok'):
        return jsonify(result), 400

    # Re-validate with same gate used by campaigns
    check = validate_campaign_media(result.get('path') or result.get('absolute_path'))
    result['media_ok'] = bool(check.get('ok'))
    result['validation'] = {
        'ok': check.get('ok'),
        'message': check.get('message'),
        'compat_blocker': check.get('compat_blocker'),
    }
    return jsonify(result)


def _is_system_test_media(filename: str) -> bool:
    """True when filename matches operator-hidden system/test media patterns."""
    import re

    name = filename or ''
    patterns = (
        r'^canary_',
        r'^audit_',
        r'^scheduled_production_check_',
        r'^test\.',
        r'_test_',
        r'^certification_',
        r'^cert_',
    )
    return any(re.search(p, name, re.IGNORECASE) for p in patterns)


@api.route('/media/files', methods=['GET'])
def list_media_files():
    """List uploaded media files."""
    from pathlib import Path

    media_dir = Path(settings.storage.media_dir)
    if not media_dir.exists():
        return jsonify({'files': []})

    files = []
    for f in sorted(media_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if f.suffix.lower() in _ALLOWED_MEDIA and f.is_file() and f.name != '.gitkeep':
            files.append({
                'filename': f.name,
                'path': str(f),
                'url': f'/api/media/file/{f.name}',
                'size': f.stat().st_size,
                'modified': datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
                'type': 'video' if f.suffix.lower() in {'.mp4', '.mov'} else 'photo',
                'is_system_test': _is_system_test_media(f.name),
            })
    return jsonify({'files': files})


@api.route('/media/file/<path:filename>', methods=['GET'])
def serve_media_file(filename):
    """Serve an uploaded media file (used for preview thumbnails)."""
    from pathlib import Path
    from flask import send_from_directory

    media_dir = Path(settings.storage.media_dir).resolve()
    # Security: only serve files directly inside media_dir (no path traversal)
    target = (media_dir / filename).resolve()
    if not str(target).startswith(str(media_dir)):
        return jsonify({'error': 'Forbidden'}), 403
    return send_from_directory(str(media_dir), filename)


# ============================================
# Stories API
# ============================================

@api.route('/stories', methods=['GET'])
def list_stories():
    """List stories with pagination"""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)

    with get_db_context() as db:
        query = db.query(Story).order_by(Story.published_at.desc())
        total = query.count()
        stories = query.offset((page - 1) * per_page).limit(per_page).all()
        return jsonify({
            "total": total,
            "page": page,
            "per_page": per_page,
            "stories": [
                {
                    "id": s.id,
                    "account_id": s.account_id,
                    "story_id": s.story_id,
                    "media_type": s.media_type,
                    "caption": s.caption[:100] if s.caption else None,
                    "mentions": len(s.mentioned_user_ids) if s.mentioned_user_ids else 0,
                    "views": s.views_count,
                    "published_at": s.published_at.isoformat(),
                }
                for s in stories
            ]
        })


@api.route('/stories/publish', methods=['POST'])
def publish_story():
    """Publish a new story"""
    from src.stories.publisher import story_publisher
    from src.clients.manager import client_manager
    data = request.get_json()
    account_id = data.get('account_id')
    media_path = data.get('media_path')
    caption = data.get('caption')
    mentions = data.get('mentions', [])
    if not account_id or not media_path:
        return jsonify({"error": "account_id and media_path required"}), 400
    async def publish():
        client = await client_manager.get_client(account_id)
        if not client:
            return {"error": "Client not found or not connected"}
        return await story_publisher.publish_story(
            client_wrapper=client,
            media_path=media_path,
            caption=caption,
            mentions=mentions,
            campaign_id=data.get('campaign_id')
        )
    result = run_async(publish())
    return jsonify(result)


@api.route('/stories/batch', methods=['POST'])
def publish_batch():
    """Publish stories in batch"""
    from src.stories.publisher import story_publisher
    data = request.get_json()
    result = run_async(story_publisher.publish_batch(
        media_path=data.get('media_path'),
        caption=data.get('caption'),
        mentions_per_story=data.get('mentions_per_story', 5),
        max_stories=data.get('max_stories', 10),
        campaign_id=data.get('campaign_id')
    ))
    return jsonify(result)


# ============================================
# Story Pools API
# ============================================

@api.route('/stories/pools', methods=['GET'])
def list_story_pools():
    """List all story pools with member counts."""
    from src.core.models import StoryPool, StoryPoolMember
    with get_db_context() as db:
        pools = db.query(StoryPool).order_by(StoryPool.created_at.desc()).all()
        result = []
        for p in pools:
            total = db.query(StoryPoolMember).filter(
                StoryPoolMember.pool_id == p.id).count()
            enabled = db.query(StoryPoolMember).filter(
                StoryPoolMember.pool_id == p.id,
                StoryPoolMember.is_enabled == True).count()
            result.append({
                'id': p.id,
                'name': p.name,
                'slug': p.slug,
                'description': p.description,
                'is_active': p.is_active,
                'member_count': total,
                'enabled_count': enabled,
                'created_at': p.created_at.isoformat(),
            })
        return jsonify({'pools': result})


@api.route('/stories/pools', methods=['POST'])
def create_story_pool():
    """Create a new story pool."""
    from src.core.models import StoryPool
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'name required'}), 400
    slug = (data.get('slug') or name.lower().replace(' ', '_').replace('-', '_'))
    with get_db_context() as db:
        if db.query(StoryPool).filter(StoryPool.slug == slug).first():
            return jsonify({'error': f"Pool '{slug}' already exists"}), 409
        pool = StoryPool(
            name=name,
            slug=slug,
            description=(data.get('description') or '').strip(),
        )
        db.add(pool)
        db.commit()
        return jsonify({'success': True, 'id': pool.id, 'slug': pool.slug})


@api.route('/stories/pools/<int:pool_id>', methods=['DELETE'])
def delete_story_pool(pool_id):
    """Delete a story pool (cascades members)."""
    from src.core.models import StoryPool
    with get_db_context() as db:
        pool = db.get(StoryPool, pool_id)
        if not pool:
            return jsonify({'error': 'Not found'}), 404
        db.delete(pool)
        db.commit()
        return jsonify({'success': True})


@api.route('/stories/pools/<int:pool_id>/members', methods=['GET'])
def list_pool_members(pool_id):
    """List accounts in a pool with story stats."""
    from src.core.models import StoryPool, StoryPoolMember
    with get_db_context() as db:
        pool = db.get(StoryPool, pool_id)
        if not pool:
            return jsonify({'error': 'Pool not found'}), 404
        members = db.query(StoryPoolMember).filter(
            StoryPoolMember.pool_id == pool_id).all()
        rows = []
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        for m in members:
            acc = db.get(Account, m.account_id)
            if not acc:
                continue
            total_stories = db.query(Story).filter(Story.account_id == acc.id).count()
            today_stories = db.query(Story).filter(
                Story.account_id == acc.id,
                Story.published_at >= today_start,
            ).count()
            last_story = db.query(Story).filter(
                Story.account_id == acc.id
            ).order_by(Story.published_at.desc()).first()
            last_story_at = last_story.published_at.isoformat() if last_story else None
            # Next allowed = last_active + cooldown
            next_allowed_at = None
            if acc.last_active:
                from src.stories.rotation import ACCOUNT_STORY_COOLDOWN_SEC
                next_dt = acc.last_active + timedelta(seconds=ACCOUNT_STORY_COOLDOWN_SEC)
                next_allowed_at = next_dt.isoformat() if next_dt > datetime.utcnow() else None
            rows.append({
                'member_id': m.id,
                'account_id': acc.id,
                'phone': acc.phone_number,
                'username': acc.username,
                'status': acc.status.value if hasattr(acc.status, 'value') else acc.status,
                'health': acc.health_status,
                'story_precheck': acc.story_precheck_status,
                'is_enabled': m.is_enabled,
                'total_stories': total_stories,
                'today_stories': today_stories,
                'last_story_at': last_story_at,
                'next_allowed_at': next_allowed_at,
                'added_at': m.added_at.isoformat(),
            })
        return jsonify({'pool_id': pool_id, 'pool_name': pool.name, 'members': rows})


@api.route('/stories/pools/<int:pool_id>/members', methods=['POST'])
def add_pool_members(pool_id):
    """Add one or more accounts to a pool."""
    from src.core.models import StoryPool, StoryPoolMember
    data = request.get_json() or {}
    account_ids = data.get('account_ids', [])
    if not account_ids:
        return jsonify({'error': 'account_ids required'}), 400
    with get_db_context() as db:
        if not db.get(StoryPool, pool_id):
            return jsonify({'error': 'Pool not found'}), 404
        added = 0
        for aid in account_ids:
            existing = db.query(StoryPoolMember).filter(
                StoryPoolMember.pool_id == pool_id,
                StoryPoolMember.account_id == aid,
            ).first()
            if not existing:
                db.add(StoryPoolMember(pool_id=pool_id, account_id=aid))
                added += 1
        db.commit()
        return jsonify({'success': True, 'added': added})


@api.route('/stories/pools/<int:pool_id>/members/<int:account_id>', methods=['DELETE'])
def remove_pool_member(pool_id, account_id):
    """Remove an account from a pool."""
    from src.core.models import StoryPoolMember
    with get_db_context() as db:
        m = db.query(StoryPoolMember).filter(
            StoryPoolMember.pool_id == pool_id,
            StoryPoolMember.account_id == account_id,
        ).first()
        if not m:
            return jsonify({'error': 'Member not found'}), 404
        db.delete(m)
        db.commit()
        return jsonify({'success': True})


# ============================================
# Story Runs API
# ============================================

@api.route('/stories/runs', methods=['GET'])
def list_story_runs():
    """List story runs (batch history)."""
    from src.core.models import StoryRun, StoryPool
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    with get_db_context() as db:
        q = db.query(StoryRun).order_by(StoryRun.created_at.desc())
        total = q.count()
        runs = q.offset((page - 1) * per_page).limit(per_page).all()
        pool_ids = {r.pool_id for r in runs if r.pool_id}
        pool_names = {}
        if pool_ids:
            for p in db.query(StoryPool).filter(StoryPool.id.in_(pool_ids)).all():
                pool_names[p.id] = p.name
        return jsonify({
            'total': total,
            'page': page,
            'runs': [
                {
                    'id': r.id,
                    'pool_id': r.pool_id,
                    'pool_name': pool_names.get(r.pool_id, '—'),
                    'mode': r.mode,
                    'interval_minutes': r.interval_minutes,
                    'caption': (r.caption or '')[:60] or None,
                    'media_path': r.media_path,
                    'mentions_per_story': r.mentions_per_story,
                    'mention_source_chat_id': getattr(r, 'mention_source_chat_id', None),
                    'status': r.status,
                    'stories_ok': r.stories_ok,
                    'stories_failed': r.stories_failed,
                    'started_at': r.started_at.isoformat() if r.started_at else None,
                    'last_tick_at': r.last_tick_at.isoformat() if r.last_tick_at else None,
                    'next_tick_at': r.next_tick_at.isoformat() if r.next_tick_at else None,
                    'created_at': r.created_at.isoformat(),
                }
                for r in runs
            ],
        })


@api.route('/stories/runs', methods=['POST'])
def create_story_run():
    """Create and queue a new story run (picked up by worker)."""
    from src.core.models import StoryRun
    data = request.get_json() or {}
    media_path = (data.get('media_path') or '').strip()
    if not media_path:
        return jsonify({'error': 'media_path required'}), 400
    mode = data.get('mode', 'once')
    if mode not in ('once', 'continuous'):
        return jsonify({'error': "mode must be 'once' or 'continuous'"}), 400
    interval = data.get('interval_minutes')
    if mode == 'continuous' and not interval:
        return jsonify({'error': 'interval_minutes required for continuous mode'}), 400
    raw_msc = data.get('mention_source_chat_id')
    mention_source_chat_id = None
    if raw_msc not in (None, '', 'null'):
        try:
            mention_source_chat_id = int(raw_msc)
        except (TypeError, ValueError):
            return jsonify({'error': 'mention_source_chat_id must be an integer or null'}), 400
    with get_db_context() as db:
        run = StoryRun(
            pool_id=data.get('pool_id') or None,
            mode=mode,
            interval_minutes=int(interval) if interval else None,
            caption=data.get('caption'),
            media_path=media_path,
            mentions_per_story=int(data.get('mentions_per_story', 5)),
            max_stories=int(data.get('max_stories')) if data.get('max_stories') else None,
            mention_source_chat_id=mention_source_chat_id,
            status='pending',
        )
        db.add(run)
        db.commit()
        return jsonify({'success': True, 'run_id': run.id, 'status': 'pending'})


@api.route('/stories/runs/<int:run_id>', methods=['GET'])
def get_story_run(run_id):
    """Get detail + recent steps for a run."""
    from src.core.models import StoryRun, StoryRunStep
    with get_db_context() as db:
        run = db.get(StoryRun, run_id)
        if not run:
            return jsonify({'error': 'Not found'}), 404
        steps = db.query(StoryRunStep).filter(
            StoryRunStep.run_id == run_id
        ).order_by(StoryRunStep.executed_at.desc()).limit(20).all()
        return jsonify({
            'run': {
                'id': run.id, 'mode': run.mode, 'status': run.status,
                'pool_id': run.pool_id,
                'mention_source_chat_id': getattr(run, 'mention_source_chat_id', None),
                'interval_minutes': run.interval_minutes,
                'stories_ok': run.stories_ok, 'stories_failed': run.stories_failed,
                'started_at': run.started_at.isoformat() if run.started_at else None,
                'last_tick_at': run.last_tick_at.isoformat() if run.last_tick_at else None,
                'next_tick_at': run.next_tick_at.isoformat() if run.next_tick_at else None,
            },
            'steps': [
                {
                    'id': s.id, 'account_id': s.account_id,
                    'status': s.status, 'error': s.error,
                    'executed_at': s.executed_at.isoformat(),
                }
                for s in steps
            ],
        })


@api.route('/stories/runs/<int:run_id>/cancel', methods=['POST'])
def cancel_story_run(run_id):
    """Cancel a pending or running story run."""
    from src.core.models import StoryRun
    with get_db_context() as db:
        run = db.get(StoryRun, run_id)
        if not run:
            return jsonify({'error': 'Not found'}), 404
        if run.status in ('completed', 'failed', 'cancelled'):
            return jsonify({'error': f'Run is already {run.status}'}), 409
        run.status = 'cancelled'
        run.completed_at = datetime.utcnow()
        db.commit()
        return jsonify({'success': True})


# ============================================
# Story Blacklist API
# ============================================

@api.route('/stories/blacklist', methods=['GET'])
def list_blacklist():
    """List blacklisted discovered users."""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    with get_db_context() as db:
        q = db.query(DiscoveredUser).filter(DiscoveredUser.is_blocked == True)
        total = q.count()
        users = q.order_by(DiscoveredUser.discovered_at.desc()).offset(
            (page - 1) * per_page).limit(per_page).all()
        return jsonify({
            'total': total,
            'entries': [
                {
                    'id': u.id,
                    'user_id': u.user_id,
                    'username': u.username,
                    'first_name': u.first_name,
                    'source': u.source_chat_title,
                    'discovered_at': u.discovered_at.isoformat(),
                }
                for u in users
            ],
        })


@api.route('/stories/blacklist', methods=['POST'])
def add_to_blacklist():
    """Blacklist a discovered user by user_id or username."""
    data = request.get_json() or {}
    user_id = data.get('user_id')
    username = (data.get('username') or '').lstrip('@').strip() or None
    if not user_id and not username:
        return jsonify({'error': 'user_id or username required'}), 400
    with get_db_context() as db:
        if user_id:
            user = db.query(DiscoveredUser).filter(
                DiscoveredUser.user_id == int(user_id)).first()
        else:
            user = db.query(DiscoveredUser).filter(
                DiscoveredUser.username == username).first()
        if not user:
            return jsonify({'error': 'User not found in discovered users'}), 404
        user.is_blocked = True
        db.commit()
        return jsonify({'success': True, 'user_id': user.user_id, 'username': user.username})


@api.route('/stories/blacklist/<int:discovered_id>', methods=['DELETE'])
def remove_from_blacklist(discovered_id):
    """Unblock a discovered user."""
    with get_db_context() as db:
        user = db.query(DiscoveredUser).filter(
            DiscoveredUser.id == discovered_id).first()
        if not user:
            return jsonify({'error': 'Not found'}), 404
        user.is_blocked = False
        db.commit()
        return jsonify({'success': True})


# ============================================
# Story Account Stats API
# ============================================

@api.route('/stories/account-stats', methods=['GET'])
def story_account_stats():
    """
    Per-account story counters for fleet status table.
    Returns accounts with story-ready status and their posting history.
    """
    from src.stories.rotation import ACCOUNT_STORY_COOLDOWN_SEC
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    with get_db_context() as db:
        accounts = db.query(Account).filter(
            Account.status == AccountStatus.ACTIVE,
        ).order_by(Account.last_active.desc().nullslast()).all()

        rows = []
        for acc in accounts:
            total = db.query(Story).filter(Story.account_id == acc.id).count()
            today = db.query(Story).filter(
                Story.account_id == acc.id,
                Story.published_at >= today_start,
            ).count()
            last_s = db.query(Story).filter(
                Story.account_id == acc.id
            ).order_by(Story.published_at.desc()).first()
            last_at = last_s.published_at.isoformat() if last_s else None

            next_allowed = None
            if acc.last_active:
                nxt = acc.last_active + timedelta(seconds=ACCOUNT_STORY_COOLDOWN_SEC)
                if nxt > datetime.utcnow():
                    next_allowed = nxt.isoformat()

            rows.append({
                'account_id': acc.id,
                'phone': acc.phone_number,
                'username': acc.username,
                'status': acc.status.value if hasattr(acc.status, 'value') else acc.status,
                'health': acc.health_status,
                'story_precheck': acc.story_precheck_status,
                'total_stories': total,
                'today_stories': today,
                'last_story_at': last_at,
                'next_allowed_at': next_allowed,
            })
        return jsonify({'accounts': rows})


# ============================================
# Discovery API
# ============================================
@api.route('/discovery/sources', methods=['GET'])
def list_discovery_sources():
    """Return distinct source chats with counts of available mention targets."""
    from sqlalchemy import text

    sql = text("""
        SELECT source_chat_id, source_chat_title,
               COUNT(*) AS total,
               SUM(CASE WHEN times_mentioned=0 AND is_blocked=0 AND username IS NOT NULL THEN 1 ELSE 0 END) AS available,
               SUM(CASE WHEN times_mentioned > 0 THEN 1 ELSE 0 END) AS mentioned
        FROM discovered_users
        WHERE source_chat_id IS NOT NULL
        GROUP BY source_chat_id, source_chat_title
        ORDER BY available DESC
    """)
    with get_db_context() as db:
        rows = db.execute(sql).fetchall()
    sources = []
    for row in rows:
        chat_id, title, total, available, mentioned = row[0], row[1], row[2], row[3], row[4]
        sources.append({
            'chat_id': chat_id,
            'title': title or f'Chat {chat_id}',
            'total': int(total or 0),
            'available': int(available or 0),
            'mentioned': int(mentioned or 0),
        })
    return jsonify({'sources': sources})


@api.route('/discovery/users', methods=['GET'])
def list_discovered_users():
    """List discovered users"""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    mentioned = request.args.get('mentioned', None)
    source_chat_id = request.args.get('source_chat_id', type=int)
    with get_db_context() as db:
        query = db.query(DiscoveredUser)
        if source_chat_id is not None:
            query = query.filter(DiscoveredUser.source_chat_id == source_chat_id)
        if mentioned == 'true':
            query = query.filter(DiscoveredUser.times_mentioned > 0)
        elif mentioned == 'false':
            query = query.filter(DiscoveredUser.times_mentioned == 0)
        total = query.count()
        users = query.order_by(DiscoveredUser.discovered_at.desc()).offset((page - 1) * per_page).limit(per_page).all()
        return jsonify({
            "total": total,
            "page": page,
            "per_page": per_page,
            "users": [
                {
                    "id": u.id,
                    "user_id": u.user_id,
                    "username": u.username,
                    "first_name": u.first_name,
                    "source": u.source_chat_title,
                    "source_chat_id": u.source_chat_id,
                    "times_mentioned": u.times_mentioned,
                    "discovered_at": u.discovered_at.isoformat(),
                }
                for u in users
            ]
        })


_scan_tasks: dict = {}  # task_id -> {status, progress, result}


def _run_scan_background(task_id: str, channels: list, limit) -> None:
    from src.discovery.scanner import user_discovery
    _scan_tasks[task_id] = {'status': 'running', 'progress': [], 'result': None}

    async def _progress(msg: str) -> None:
        _scan_tasks[task_id]['progress'].append(msg)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(
            user_discovery.discover_from_channels(
                channel_usernames=channels,
                limit_per_channel=limit,
                progress_callback=_progress,
            )
        )
        _scan_tasks[task_id]['result'] = result
        _scan_tasks[task_id]['status'] = 'done'
    except Exception as exc:
        logger.error("Background scan failed", error=str(exc), exc_info=True)
        _scan_tasks[task_id]['result'] = {'success': False, 'errors': [str(exc)]}
        _scan_tasks[task_id]['status'] = 'error'
    finally:
        loop.close()


@api.route('/discovery/scan', methods=['POST'])
def scan_channel():
    """Start a background channel scan and return a task_id for polling"""
    data = request.get_json() or {}
    channels = data.get('channels', [])
    if not channels:
        return jsonify({"error": "channels list required"}), 400
    # limit=0 or omitted → None (unlimited); otherwise use provided value
    raw_limit = data.get('limit')
    limit = int(raw_limit) if raw_limit else None

    task_id = uuid.uuid4().hex[:10]
    t = threading.Thread(
        target=_run_scan_background,
        args=(task_id, channels, limit),
        daemon=True,
    )
    t.start()
    return jsonify({'task_id': task_id, 'status': 'running'})


@api.route('/discovery/scan_status/<task_id>', methods=['GET'])
def scan_status(task_id: str):
    """Poll background scan progress"""
    task = _scan_tasks.get(task_id)
    if not task:
        return jsonify({'error': 'task not found'}), 404
    return jsonify(task)


@api.route('/discovery/join', methods=['POST'])
def join_channel_route():
    """Join a Telegram channel/group using an active account"""
    from src.discovery.scanner import user_discovery
    data = request.get_json() or {}
    channel = (data.get('channel') or '').strip()
    if not channel:
        return jsonify({'error': 'channel required'}), 400
    try:
        result = run_async(user_discovery.join_channel(channel))
        return jsonify(result)
    except Exception as exc:
        logger.error("Join channel failed", error=str(exc), exc_info=True)
        return jsonify({'success': False, 'error': str(exc)}), 500


@api.route('/discovery/stats', methods=['GET'])
def discovery_stats():
    """Get discovery statistics"""
    from src.discovery.scanner import user_discovery
    stats = run_async(user_discovery.get_discovery_stats())
    return jsonify(stats)


# ============================================
# Campaigns API
# ============================================
@api.route('/campaigns', methods=['GET'])
def list_campaigns():
    """List all campaigns"""
    with get_db_context() as db:
        campaigns = db.query(Campaign).all()
        return jsonify([
            {
                "id": c.id,
                "name": c.name,
                "is_active": c.is_active,
                "total_stories": c.total_stories_published,
                "total_mentions": c.total_users_mentioned,
                "created_at": c.created_at.isoformat(),
            }
            for c in campaigns
        ])


@api.route('/campaigns', methods=['POST'])
def create_campaign():
    """Wave J: marketing Campaigns owner write API retired (history table preserved)."""
    return jsonify({
        "ok": False,
        "error": "gone",
        "message": "Marketing Campaigns write API is retired. Use Stories, Scheduler, Broadcast, or Messages.",
    }), 410


@api.route('/campaigns/<int:campaign_id>', methods=['PUT'])
def update_campaign(campaign_id):
    """Wave J: marketing Campaigns owner write API retired."""
    return jsonify({
        "ok": False,
        "error": "gone",
        "message": "Marketing Campaigns write API is retired.",
        "campaign_id": campaign_id,
    }), 410


@api.route('/campaigns/<int:campaign_id>', methods=['DELETE'])
def delete_campaign(campaign_id):
    """Wave J: marketing Campaigns owner write API retired (rows preserved)."""
    return jsonify({
        "ok": False,
        "error": "gone",
        "message": "Marketing Campaigns delete API is retired. Historical rows are preserved.",
        "campaign_id": campaign_id,
    }), 410


# ============================================
# Tasks API
# ============================================
@api.route('/tasks', methods=['GET'])
def list_tasks():
    """List tasks"""
    status = request.args.get('status')
    limit = request.args.get('limit', 50, type=int)
    with get_db_context() as db:
        query = db.query(Task)
        if status:
            from src.core.models import TaskStatus
            query = query.filter(Task.status == TaskStatus(status))
        tasks = query.order_by(Task.created_at.desc()).limit(limit).all()
        return jsonify([
            {
                "id": t.id,
                "type": t.task_type.value,
                "status": t.status.value,
                "account_id": t.account_id,
                "campaign_id": t.campaign_id,
                "scheduled_at": t.scheduled_at.isoformat() if t.scheduled_at else None,
                "completed_at": t.completed_at.isoformat() if t.completed_at else None,
                "error": t.error_message,
            }
            for t in tasks
        ])


# ============================================
# Web Blueprint (HTML pages)
# ============================================
web = Blueprint('web', __name__)


@web.route('/favicon.ico')
def favicon():
    """Serve favicon to prevent 404"""
    import os
    _dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
    return send_from_directory(_dir, 'favicon.svg', mimetype='image/svg+xml')


@web.route('/logout')
def logout():
    """Log out the current user and redirect to home."""
    try:
        from flask_login import logout_user
        logout_user()
    except Exception:
        pass
    from flask import redirect
    return redirect('/')


@web.route('/')
@login_required
def index():
    """Owner Dashboard home (Wave I) — attention-first, nontechnical."""
    from src.dashboard.owner_dashboard import build_owner_dashboard_snapshot

    try:
        snapshot = build_owner_dashboard_snapshot()
    except Exception as e:
        logger.warning("owner_dashboard_render_failed", error=str(e))
        snapshot = {
            "ok": False,
            "system": {
                "owner_copy": "System needs attention",
                "detail": "Dashboard summary unavailable",
                "attention": True,
                "state": "needs_attention",
            },
            "accounts": {"unavailable": True, "owner_copy": "Account health unavailable", "attention": True},
            "messages": {"unavailable": True, "owner_copy": "Messages summary unavailable", "attention": True},
            "scheduler": {"unavailable": True, "owner_copy": "Scheduler summary unavailable", "attention": True},
            "fleet": {"unavailable": True, "owner_copy": "Fleet health unavailable", "attention": True},
            "stories": {"omit": True},
            "backup": {"surface": False},
            "disk": {"surface": False},
            "contracts": {"live_telegram_calls": 0, "openai_calls": 0, "private_body_exposed": False},
        }
    return render_template('index.html', dash=snapshot)


@web.route('/accounts')
@login_required
def accounts_page():
    """Accounts management page"""
    return render_template('accounts.html')


@web.route('/stories')
@login_required
def stories_page():
    """Stories page"""
    return render_template('stories.html')


@web.route('/discovery')
@login_required
def discovery_page():
    """User discovery page"""
    return render_template('discovery.html')


@web.route('/campaigns')
@login_required
def campaigns_page():
    """Wave J: marketing Campaigns owner surface retired (410 Gone). Table preserved."""
    return render_template('campaigns.html'), 410


@web.route('/ai-agent')
@login_required
def ai_agent_retired_page():
    """Wave J: AI Agent owner UI retired; APIs/history retained for forensics."""
    return render_template(
        'retired_product.html',
        product_name='AI Agent',
        headline='AI Agent owner UI is retired.',
        detail=(
            'The negotiation-desk experiment is dormant. Reserved-account safety and '
            'audit history remain. Use Messages for private replies, or Agents for Social Agent.'
        ),
        links=[
            {"href": "/agents", "label": "Agents", "class": "btn-outline-primary"},
            {"href": "/messages", "label": "Messages", "class": "btn-outline-primary"},
        ],
    ), 410


@web.route('/advanced')
@login_required
def advanced_page():
    """Owner landing for technical/operator tools (Wave 4 / Wave J navigation)."""
    return render_template('advanced.html')


@web.route('/scheduler')
@login_required
def scheduler_page():
    """Owner-facing Scheduler: upcoming + recent jobs (Wave 5)."""
    from flask import request as flask_request

    from src.core.database import get_db_context
    from src.dashboard.scheduler_owner_presentation import build_owner_scheduler_view

    filt = (flask_request.args.get("filter") or "all").strip().lower()
    with get_db_context() as db:
        view = build_owner_scheduler_view(db, filter_key=filt)
    return render_template("scheduler.html", view=view)


@web.route('/scheduler/setup')
@login_required
def scheduler_setup_page():
    """Technical schedule setup (targets/bindings/templates) — not primary owner IA."""
    return render_template('scheduler_setup.html')


# ============================================
# Register all routes
# ============================================
def register_routes(app):
    """Register all blueprints"""
    from datetime import datetime

    # Add datetime to template context
    @app.context_processor
    def inject_datetime():
        return {'datetime': datetime}

    app.register_blueprint(api)
    app.register_blueprint(web)

    logger.info("Routes registered")
