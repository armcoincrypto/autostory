"""
Dashboard Routes - API and Web endpoints
"""
import asyncio
import hmac
import json
import threading
import os
import random
import sys
import time
import urllib.error
import urllib.request
from functools import wraps
from datetime import datetime
from typing import Any, List, Optional

from flask import Blueprint, jsonify, request, render_template, send_from_directory
from flask_login import login_required, current_user
import structlog

_here = os.path.dirname(os.path.abspath(__file__))
_project_root = os.path.abspath(os.path.join(_here, "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
from config.settings import settings
from src.core.models import Account, Story, DiscoveredUser, Campaign, Task, AccountStatus, StoryTemplate, MentionBlacklist, UploadedMentionSource, UploadedMentionEntry, StorySchedule
from src.core.database import get_db_context, run_with_sqlite_lock_retry

logger = structlog.get_logger(__name__)


def _dt_iso_optional(val: Any) -> Optional[str]:
    """
    API timestamp field: accept datetime/date from ORM or ISO strings from SQLite/text columns.
    None / empty string -> None.
    """
    if val is None:
        return None
    if isinstance(val, str):
        s = val.strip()
        return s if s else None
    iso = getattr(val, "isoformat", None)
    if callable(iso):
        try:
            return iso()
        except (TypeError, ValueError):
            pass
    s = str(val).strip()
    return s if s else None


# Fleet background healthcheck: one asyncio.run per chunk (each run gets a fresh event loop). No global fleet cap.
HEALTHCHECK_BG_CHUNK_SIZE = int(os.environ.get("HEALTHCHECK_BG_CHUNK_SIZE", "30"))
HEALTHCHECK_BG_CHUNK_TIMEOUT_SEC = float(os.environ.get("HEALTHCHECK_BG_CHUNK_TIMEOUT_SEC", "900"))
_HEALTHCHECK_REMAINING_IDS_CAP = int(os.environ.get("HEALTHCHECK_BG_MAX_REMAINING_IDS_STORED", "2000"))

HEALTHCHECK_RESPONSE_NOTE = (
    "General health only: connect/auth/get_me. Story readiness shown separately from DB snapshot."
)


def _sync_healthcheck_timeout_sec(num_accounts: int) -> float:
    """Wall-clock budget for synchronous health endpoints (small batches only)."""
    explicit = os.environ.get("HEALTHCHECK_SYNC_TIMEOUT_SEC")
    if explicit is not None and str(explicit).strip() != "":
        return float(explicit)
    n = max(1, min(int(num_accounts), 15))
    return min(900.0, 90.0 + n * 55.0)


def _healthcheck_status_counts(results: list) -> dict:
    counts: dict[str, int] = {}
    for row in results or []:
        if not isinstance(row, dict):
            continue
        s = str(row.get("status") or "unknown").strip()
        counts[s] = counts.get(s, 0) + 1
    return counts


def _story_db_snapshot_from_account(acc: Account) -> dict:
    """
    Read-only story snapshot: same DB-backed semantics as /api/accounts (story_ui_status, story_reason,
    story_available_label via get_story_availability; safety reason via get_story_safety_decision).
    Does not call Telegram or mutate story state.
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
    elif ui == "frozen" or code in ("story_frozen", "story_blocked"):
        state = "frozen"
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
        "blocked_until": blocked_iso,
        "precheck_stale": pre_stale,
        "safety_reason": code or None,
    }


def _enrich_health_results_with_db_story_state(results: list) -> None:
    """
    For each general-health result row, attach story_from_db (read-only DB snapshot; no story health calls).
    """
    if not results:
        return
    ids: List[int] = []
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


def _admin_token_configured() -> str | None:
    """Read admin token from env (primary) or settings. Ensures runtime value."""
    tok = (os.environ.get("DASHBOARD_ADMIN_TOKEN") or "").strip() or None
    if tok:
        return tok
    tok = (getattr(settings.dashboard, "admin_token", None) or "").strip() or None
    return tok


def _is_production_env() -> bool:
    """Treat as production unless explicitly development. Safe default for auth."""
    env_val = (getattr(settings, "environment", None) or "").strip().lower()
    flask_env = (os.environ.get("FLASK_ENV") or "").strip().lower()
    return env_val != "development" and flask_env != "development"


def _admin_api_allowed() -> bool:
    """
    Admin-only API gate. FAIL CLOSED: require valid token or session unless
    DASHBOARD_ALLOW_INSECURE_ADMIN_API=true AND explicitly in development.
    """
    cfg = _admin_token_configured()
    allow_insecure = getattr(settings.dashboard, "allow_insecure_admin_api", False) or (
        os.environ.get("DASHBOARD_ALLOW_INSECURE_ADMIN_API", "").lower() in ("true", "1", "yes")
    )
    is_production = _is_production_env()

    # Explicit dev bypass (must be opt-in, NEVER in production)
    if allow_insecure and not is_production:
        return True

    # No token configured: in production always deny. In dev, allow only if insecure bypass set.
    if not cfg:
        if is_production:
            return False
        return allow_insecure

    # Token configured: require valid token (constant-time) or logged-in admin
    token = (request.headers.get("X-Admin-Token") or "").strip() or None
    if token and cfg:
        expected = cfg.encode("utf-8", errors="replace")
        received = token.encode("utf-8", errors="replace")
        if len(expected) == len(received) and hmac.compare_digest(expected, received):
            return True
    try:
        return bool(getattr(current_user, "is_authenticated", False) and getattr(current_user, "is_admin", False))
    except Exception:
        return False


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
        # Keep local: list, single-account, session-audit; proxy /dialogs to server
        if "/dialogs" in path:
            pass  # proxy dialogs so server fetches from Telegram
        elif "/session-audit" in path:
            return None  # session-audit must run locally (checks disk)
        else:
            return None
    proxy_url = proxy_url.rstrip("/")
    url = proxy_url + request.full_path
    try:
        body = request.get_data() or None
        headers = {}
        _adm = (request.headers.get("X-Admin-Token") or "").strip()
        if _adm:
            headers["X-Admin-Token"] = _adm
        if body and request.content_type:
            headers["Content-Type"] = request.content_type
        elif body:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, headers=headers, method=request.method)
        with urllib.request.urlopen(req, timeout=125) as r:
            raw = r.read().decode()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                # Remote returned HTML or non-JSON (e.g. error page)
                resp = jsonify({
                    "success": False,
                    "error": "Proxy target returned non-JSON response (e.g. HTML error page). Check backend and proxy URL."
                })
                resp.status_code = 502
                return resp
            resp = jsonify(data)
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
    """Run async function in sync context; cancel stray tasks before loop close."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        except Exception:
            logger.debug("asyncio_loop_cleanup_skipped", exc_info=True)
        finally:
            loop.close()


def run_async_with_timeout(coro, timeout_sec: float):
    """Run async function in sync context with timeout; cancel stray tasks before loop close (Telethon hygiene)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(asyncio.wait_for(coro, timeout=timeout_sec))
    finally:
        try:
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        except Exception:
            logger.debug("asyncio_loop_cleanup_skipped", exc_info=True)
        finally:
            loop.close()


def admin_api_required(fn):
    @wraps(fn)
    def _wrapped(*args, **kwargs):
        if not _admin_api_allowed():
            return jsonify({"success": False, "error": "Admin only"}), 403
        return fn(*args, **kwargs)
    return _wrapped

# ============================================
# API Blueprint
# ============================================
api = Blueprint('api', __name__, url_prefix='/api')


@api.before_request
def maybe_proxy_api():
    """Proxy all /api/* to server when DASHBOARD_RUN_NOW_PROXY_URL is set (Dashboard, Accounts, Stories, etc.)."""
    rv = _proxy_api_to_server()
    if rv is not None:
        return rv


@api.before_request
def require_admin_api():
    """Fail-closed: require admin token or session for all /api/* except /api/health."""
    if request.endpoint == "api.health_check":
        return None  # Allow /api/health without auth
    if not _admin_api_allowed():
        return jsonify({"success": False, "error": "Admin only"}), 403
    return None


@api.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({"status": "healthy", "service": "storyfleet"})


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


# ============================================
# Accounts API
# ============================================
def _identity_verify_suggested(account: Any, _canonical_exists: set[int] | None = None) -> bool:
    """
    Operator hint only (never auto-action): provable session/row tension.
    True when canonical session file exists AND either:
    - Last health is auth_required/error/deleted/banned (file present but live path not OK), or
    - Health says alive but user_id was never stored (inconsistent with a successful identity sync).

    NOT flagged: alive + user_id set + username null (valid accounts without a public @username).
    """
    from src.core.session_paths import account_has_canonical_session

    if not account_has_canonical_session(account, _canonical_exists=_canonical_exists):
        return False

    hs = getattr(account, "health_status", None) or ""
    hs = str(hs).strip()
    uid = getattr(account, "user_id", None)

    bad_health_with_file = frozenset({"auth_required", "error", "deleted", "banned"})
    if hs in bad_health_with_file:
        return True

    if hs == "alive" and uid is None:
        return True

    return False


def _identity_fields_for_api(account: Any, canonical_exists: set[int] | None = None) -> dict:
    """Read-only identity audit for operators: DB columns from last check + fallback hint if never audited."""
    st = getattr(account, "identity_audit_status", None)
    reason = getattr(account, "identity_audit_reason", None)
    at = getattr(account, "identity_audit_at", None)
    if st:
        suggested = st in ("metadata_stale", "identity_mismatch", "session_invalid")
    else:
        suggested = _identity_verify_suggested(account, canonical_exists)
    return {
        "identity_status": st,
        "identity_reason": reason,
        "identity_checked_at": _dt_iso_optional(at),
        "identity_verify_suggested": suggested,
    }


def _story_precheck_queue_detail(account: Any, story_ui_status: str | None) -> str | None:
    """
    When story_ui_status is needs_precheck, distinguish never-run vs post-TTL stale for operators.
    story_safety_reason may still be story_precheck_failed in both cases — this field disambiguates.
    """
    if (story_ui_status or "") != "needs_precheck":
        return None
    if getattr(account, "story_precheck_checked_at", None) is None:
        return "precheck_never_run"
    return "precheck_post_ttl_expired"


@api.route('/accounts', methods=['GET'])
def list_accounts():
    """List all accounts. Pure DB read only (no Telegram). Supports ?purpose=messaging|autostory and ?limit=500.
    By default returns a plain array (backward compatible). Use ?summary=1 to get { accounts, summary }.
    has_session = True only when canonical session file (or session_path file) exists; do not trust session_string alone.
    Returns: story_status, story_status_reason, story_status_checked_at, story_blocked_until, health_status, session_path,
    plus story_ui_status, story_available_label, story_reason, is_story_ready from get_story_availability."""
    from src.core.session_paths import (
        account_has_canonical_session,
        get_existing_canonical_account_ids,
        get_session_readiness,
        get_story_availability,
    )
    limit = min(500, max(1, request.args.get("limit", 100, type=int)))
    want_summary = request.args.get("summary", "").strip() in ("1", "true", "yes")
    try:
        canonical_exists = get_existing_canonical_account_ids()
        with get_db_context() as db:
            try:
                q = db.query(Account).order_by(Account.id).limit(limit)
                accounts = q.all()
            except Exception as e:
                logger.warning("Accounts query failed (missing column?), falling back", error=str(e))
                from sqlalchemy import text
                rows = db.execute(text(
                    "SELECT id, phone_number, username, first_name, status, last_active, stories_today FROM accounts LIMIT :n"
                ), {"n": limit}).fetchall()
                result = []
                purpose_filter = request.args.get("purpose")
                for r in rows:
                    p = "both"
                    if purpose_filter == "messaging" and p == "autostory":
                        continue
                    if purpose_filter == "autostory" and p == "messaging":
                        continue
                    result.append({
                        "id": r.id,
                        "phone_number": r.phone_number,
                        "username": r.username,
                        "first_name": r.first_name,
                        "status": getattr(r.status, "value", r.status) if getattr(r, "status", None) is not None else "inactive",
                        "purpose": "both",
                        "last_active": _dt_iso_optional(getattr(r, "last_active", None)),
                        "stories_today": getattr(r, "stories_today", 0) or 0,
                        "has_session": False,
                        "session_readiness": "needs_reimport",
                        "health_status": None,
                        "health_reason": None,
                        "health_checked_at": None,
                    })
                summary = {"total": len(result), "active": 0, "auth_required": 0, "flood_wait": 0, "frozen": 0, "banned": 0, "other": 0, "canonical_session_ready": 0, "missing_canonical_session": len(result)}
                if want_summary:
                    return jsonify({"accounts": result, "summary": summary})
                return jsonify(result)
            purpose_filter = request.args.get("purpose")
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
                session_readiness = get_session_readiness(a, _canonical_exists=canonical_exists)
                story_avail = get_story_availability(a, _canonical_exists=canonical_exists)
                try:
                    from src.core.warmup import format_warmup_for_ui
                    warmup_info = format_warmup_for_ui(a)
                except Exception:
                    warmup_info = {}
                result.append({
                    "id": a.id,
                    "phone_number": a.phone_number,
                    "username": a.username,
                    "first_name": a.first_name,
                    "status": getattr(a.status, "value", str(a.status)) if a.status is not None else "inactive",
                    "purpose": p,
                    "last_active": _dt_iso_optional(getattr(a, "last_active", None)),
                    "stories_today": a.stories_today if a.stories_today is not None else 0,
                    "has_session": account_has_canonical_session(a, _canonical_exists=canonical_exists),
                    "session_readiness": session_readiness,
                    "session_path": getattr(a, "session_path", None),
                    "health_status": getattr(a, "health_status", None),
                    "health_reason": getattr(a, "health_reason", None),
                    "health_checked_at": _dt_iso_optional(getattr(a, "health_checked_at", None)),
                    "story_status": getattr(a, "story_status", None),
                    "story_status_reason": getattr(a, "story_status_reason", None),
                    "story_status_checked_at": _dt_iso_optional(getattr(a, "story_status_checked_at", None)),
                    "story_blocked_until": _dt_iso_optional(getattr(a, "story_blocked_until", None)),
                    "story_ui_status": story_avail["story_ui_status"],
                    "story_available_label": story_avail["story_available_label"],
                    "story_reason": story_avail["story_reason"],
                    "is_story_ready": story_avail["is_story_ready"],
                    "story_precheck_stale": story_avail.get("story_precheck_stale", False),
                    "story_precheck_queue_detail": _story_precheck_queue_detail(a, story_avail.get("story_ui_status")),
                    "imported_at": _dt_iso_optional(getattr(a, "imported_at", None)),
                    "last_story_attempt_at": _dt_iso_optional(getattr(a, "last_story_attempt_at", None)),
                    "warmup_status": (warmup_info.get("warmup_status") if warmup_info else None) or getattr(a, "warmup_status", None),
                    "warmup_label": warmup_info.get("warmup_label"),
                    "warmup_block_reason": warmup_info.get("warmup_block_reason"),
                    "story_precheck_status": getattr(a, "story_precheck_status", None),
                    "story_precheck_reason": getattr(a, "story_precheck_reason", None),
                    "profile_capability_status": getattr(a, "profile_capability_status", None),
                    "profile_capability_reason": getattr(a, "profile_capability_reason", None),
                    **_identity_fields_for_api(a, canonical_exists),
                })
                try:
                    from src.core.safety_policy import get_story_safety_decision, get_account_risk_level
                    decision = get_story_safety_decision(a, _canonical_exists=canonical_exists)
                    result[-1]["risk_level"] = get_account_risk_level(a)
                    result[-1]["story_safety_allowed"] = decision.allowed
                    result[-1]["story_safety_reason"] = decision.reason_code
                    result[-1]["story_safety_human_reason"] = decision.human_reason
                    result[-1]["manual_review_required"] = bool(getattr(a, "manual_review_required", False))
                except Exception:
                    result[-1]["risk_level"] = None
                    result[-1]["story_safety_allowed"] = None
                    result[-1]["story_safety_reason"] = None
                    result[-1]["story_safety_human_reason"] = None
                    result[-1]["manual_review_required"] = False
            if want_summary:
                summary = _accounts_summary_from_result(result)
                return jsonify({"accounts": result, "summary": summary})
            return jsonify(result)
    except Exception as e:
        logger.exception("list_accounts failed")
        return jsonify({"error": "Failed to load accounts", "detail": str(e)}), 500


@api.route('/accounts/session-audit', methods=['GET'])
def session_audit():
    """
    Audit canonical session files vs DB. Classifies every account into:
    session_ready, missing_canonical_session, needs_reimport, auth_required,
    frozen_story, story_rate_limited, restricted, other.
    """
    from datetime import datetime
    from sqlalchemy import text
    from src.core.session_paths import get_canonical_session_path, get_sessions_dir, classify_account_readiness, recommended_action

    def _dict_row(r):
        return {
            "id": r[0],
            "phone_number": r[1],
            "status": r[2],
            "health_status": r[3],
            "session_path": r[4],
            "story_status": r[5],
            "story_blocked_until": r[6],
            "story_status_reason": r[7] if len(r) > 7 else None,
        }

    try:
        with get_db_context() as db:
            rows = db.execute(text("""
                SELECT id, phone_number, status, health_status, session_path, story_status, story_blocked_until, story_status_reason
                FROM accounts
                ORDER BY id
            """)).fetchall()
        sessions_dir = get_sessions_dir()
        by_classification = {}
        details = []
        for r in rows:
            a = _dict_row(r)
            aid = a["id"]
            canonical = get_canonical_session_path(aid)
            exists = canonical.is_file()
            # Build minimal account-like object for classify
            class _Acc:
                pass
            acc = _Acc()
            acc.id = aid
            acc.session_path = a.get("session_path")
            acc.health_status = a.get("health_status")
            acc.status = type("S", (), {"value": a.get("status")})() if a.get("status") else None
            acc.story_status = a.get("story_status")
            acc.story_blocked_until = a.get("story_blocked_until")
            cl = classify_account_readiness(acc)
            act = recommended_action(cl)
            by_classification[cl] = by_classification.get(cl, 0) + 1
            details.append({
                "id": aid,
                "phone_number": a.get("phone_number") or a.get("phone"),
                "health_status": a.get("health_status"),
                "session_path": a.get("session_path"),
                "canonical_exists": exists,
                "story_status": a.get("story_status"),
                "story_blocked_until": str(a.get("story_blocked_until")) if a.get("story_blocked_until") else None,
                "classification": cl,
                "action": act,
            })
        return jsonify({
            "sessions_dir": str(sessions_dir),
            "total_audited": len(rows),
            "by_classification": by_classification,
            "details": details,
        })
    except Exception as e:
        logger.exception("session_audit failed")
        return jsonify({"error": str(e)}), 500


async def _story_precheck_one_account(account_id: int) -> dict:
    """
    Run full story precheck flow for one account in a single event loop.
    Acquire client, run CanSendStoryRequest, persist result, disconnect.
    Prevents "event loop must not change after connection" by keeping
    get_client + precheck + disconnect in one async function.
    """
    from src.clients.manager import client_manager
    from src.stories.precheck import run_story_precheck, persist_precheck_result

    wrapper, err = await client_manager.get_fresh_client_for_story_publish(account_id)
    if err or not wrapper:
        return {
            "account_id": account_id,
            "status": "failed_check",
            "reason": err or "no_client",
            "retry_after_seconds": None,
            "checked_at": None,
        }
    try:
        precheck = await run_story_precheck(wrapper.client, account_id)
        persist_precheck_result(
            account_id,
            precheck["status"],
            precheck.get("reason", ""),
            precheck.get("retry_after_seconds"),
        )
        return {
            "account_id": account_id,
            "status": precheck["status"],
            "reason": precheck.get("reason", ""),
            "retry_after_seconds": precheck.get("retry_after_seconds"),
            "checked_at": precheck.get("checked_at"),
        }
    finally:
        # Pooled clients stay connected; ephemeral file-backed clients must disconnect.
        if getattr(wrapper, "_precheck_disconnect_after", False):
            try:
                await wrapper.disconnect()
            except Exception as e:
                logger.debug("story_precheck disconnect", account_id=account_id, error=str(e))


@api.route('/accounts/story-precheck', methods=['POST'])
@admin_api_required
def story_precheck_audit():
    """
    Run CanSendStoryRequest for account(s) to verify story eligibility before publishing.
    Body: { "account_ids": [1,2,3], "canary_batch_ok": false } - canary_batch_ok=true required for >1 account.
    Does NOT send any story; only checks Telegram API.
    Whole flow (get client, precheck, disconnect) runs in one event loop per account.
    """
    from src.core.session_paths import account_has_canonical_session
    from src.core.risk_events import record_risk_event, count_events_last_hour, EVENT_PRECHECK_RUN

    data = request.get_json() or {}
    account_ids = data.get("account_ids")
    canary_batch_ok = data.get("canary_batch_ok") in (True, "true", "1", "yes")
    canary_size = int(getattr(settings.warmup, "canary_default_batch_size", 1) or 1)
    require_canary = bool(getattr(settings.warmup, "canary_batch_ok_required", True))
    max_prechecks = int(getattr(settings.warmup, "max_prechecks_per_hour", 20) or 20)

    recent_prechecks = count_events_last_hour(EVENT_PRECHECK_RUN)
    remaining_capacity = max(0, max_prechecks - recent_prechecks)

    def _precheck_reject(*, status_code: int, error: str, eligible_count: int = 0, skipped_no_session: int = 0):
        return jsonify({
            "success": False,
            "error": error,
            "recent_count": recent_prechecks,
            "max_per_hour": max_prechecks,
            "remaining_capacity": remaining_capacity,
            "processed_count": 0,
            "skipped_count": skipped_no_session,
            "eligible_account_count": eligible_count,
            "nothing_processed": True,
        }), status_code

    if recent_prechecks >= max_prechecks:
        return _precheck_reject(
            status_code=429,
            error=f"Max prechecks per hour reached: {recent_prechecks}. Limit {max_prechecks}. Retry later.",
        )

    with get_db_context() as db:
        if account_ids and isinstance(account_ids, list):
            account_ids = [int(x) for x in account_ids if isinstance(x, (int, str)) and str(x).isdigit()]
        if account_ids:
            accounts_queried = db.query(Account).filter(Account.id.in_(account_ids)).all()
        else:
            accounts_queried = db.query(Account).all()

    accounts_eligible = [a for a in accounts_queried if account_has_canonical_session(a)]
    skipped_no_session_count = len(accounts_queried) - len(accounts_eligible)

    if require_canary and not canary_batch_ok and len(accounts_eligible) > canary_size:
        return _precheck_reject(
            status_code=400,
            error=f"Canary mode: max {canary_size} account(s) unless canary_batch_ok=true. Use canary_batch_ok to run more.",
            eligible_count=len(accounts_eligible),
            skipped_no_session=skipped_no_session_count,
        )

    if len(accounts_eligible) + recent_prechecks > max_prechecks:
        return _precheck_reject(
            status_code=429,
            error=(
                f"Would exceed precheck cap: {len(accounts_eligible)} + {recent_prechecks} > {max_prechecks}. "
                "Reduce accounts or wait."
            ),
            eligible_count=len(accounts_eligible),
            skipped_no_session=skipped_no_session_count,
        )

    results = []
    for a in accounts_eligible:
        try:
            r = run_async_with_timeout(
                _story_precheck_one_account(a.id),
                timeout_sec=60.0,
            )
            results.append(r)
            record_risk_event(a.id, EVENT_PRECHECK_RUN, "audit")
        except Exception as e:
            logger.warning("story_precheck account %s failed", a.id, error=str(e))
            results.append({
                "account_id": a.id,
                "status": "failed_check",
                "reason": str(e)[:255],
                "retry_after_seconds": None,
                "checked_at": None,
            })
    return jsonify({
        "success": True,
        "processed_count": len(results),
        "skipped_count": skipped_no_session_count,
        "skipped_no_session_count": skipped_no_session_count,
        "eligible_account_count": len(accounts_eligible),
        "nothing_processed": len(results) == 0,
        "recent_count": recent_prechecks,
        "max_per_hour": max_prechecks,
        "remaining_capacity": remaining_capacity,
        "results": results,
    })


@api.route('/accounts/story-precheck-candidates', methods=['GET'])
@admin_api_required
def story_precheck_candidates():
    """
    Operator helper: rebuild a precheck queue from live API/DB (sorted unique IDs only).
    Includes accounts with story_ui_status=needs_precheck, canonical session, status=active.
    Use POST /accounts/story-precheck; only advance/remove local queue items when response success and processed_count > 0.
    Query: limit (max 500), purpose (messaging|autostory) same semantics as GET /api/accounts.
    """
    from src.core.session_paths import (
        account_has_canonical_session,
        get_existing_canonical_account_ids,
        get_story_availability,
    )

    limit = min(500, max(1, request.args.get("limit", 500, type=int)))
    purpose_filter = request.args.get("purpose")
    canonical_exists = get_existing_canonical_account_ids()
    matched: list[int] = []
    with get_db_context() as db:
        accounts = db.query(Account).order_by(Account.id).all()
    for a in accounts:
        try:
            p = getattr(a, "purpose", None) or "both"
        except Exception:
            p = "both"
        if purpose_filter == "messaging" and p == "autostory":
            continue
        if purpose_filter == "autostory" and p == "messaging":
            continue
        st_val = getattr(a.status, "value", str(a.status or "")) or ""
        if st_val != "active":
            continue
        if not account_has_canonical_session(a, _canonical_exists=canonical_exists):
            continue
        story_avail = get_story_availability(a, _canonical_exists=canonical_exists)
        if story_avail.get("story_ui_status") != "needs_precheck":
            continue
        matched.append(a.id)
    matched = sorted(set(matched))
    truncated = len(matched) > limit
    out = matched[:limit]
    return jsonify({
        "success": True,
        "account_ids": out,
        "count": len(out),
        "matched_total": len(matched),
        "limit": limit,
        "truncated": truncated,
        "nothing_to_run": len(out) == 0,
    })


def _accounts_summary_from_result(result: list) -> dict:
    """
    Build summary from pre-computed result rows (avoids re-calling get_story_availability,
    get_session_readiness, format_warmup_for_ui per account).
    """
    summary = {
        "total": len(result),
        "active": 0,
        "auth_required": 0,
        "flood_wait": 0,
        "frozen": 0,
        "banned": 0,
        "other": 0,
        "story_available": 0,
        "story_ok": 0,
        "story_frozen": 0,
        "story_rate_limited": 0,
        "canonical_session_ready": 0,
        "missing_canonical_session": 0,
        "general_healthy": 0,
        "warmup_pending": 0,
    }
    for r in result:
        hs = r.get("health_status") or ""
        st = r.get("status") or "inactive"
        if hs == "alive":
            summary["general_healthy"] += 1
        if hs == "alive" or st == "active":
            summary["active"] += 1
        elif hs == "frozen" or st == "flood_wait":
            summary["frozen"] += 1
        elif hs == "auth_required" or st == "auth_required":
            summary["auth_required"] += 1
        elif hs == "banned" or st == "banned":
            summary["banned"] += 1
        else:
            summary["other"] += 1
        story_ui = r.get("story_ui_status") or ""
        if story_ui == "ready":
            summary["story_ok"] += 1
        elif story_ui in ("frozen", "restricted"):
            summary["story_frozen"] += 1
        elif story_ui == "rate_limited":
            summary["story_rate_limited"] += 1
        if r.get("is_story_ready"):
            summary["story_available"] += 1
        sr = r.get("session_readiness") or ""
        if sr == "canonical_ok":
            summary["canonical_session_ready"] += 1
        elif sr in ("missing_canonical", "needs_reimport"):
            summary["missing_canonical_session"] += 1
        ws = r.get("warmup_status") or ""
        block = r.get("warmup_block_reason") or ""
        if ws in ("new", "warming") and block:
            summary["warmup_pending"] += 1
    return summary


def _accounts_summary(accounts: list) -> dict:
    """
    Build counts summary using get_story_availability for story-ready/rate-limited/frozen.
    Used by session_audit and other callers; list_accounts uses _accounts_summary_from_result.
    - general_healthy: health_status=alive (Telegram API responds)
    - canonical_session_ready: canonical session file exists
    - story_available: is_story_ready (session + active + health + story ok + not blocked + warmup)
    - warmup_pending: new/warming; blocked from story posting
    """
    from src.core.session_paths import get_session_readiness, get_story_availability
    from src.core.warmup import format_warmup_for_ui
    summary = {
        "total": len(accounts), "active": 0, "auth_required": 0, "flood_wait": 0, "frozen": 0,
        "banned": 0, "other": 0, "story_available": 0, "story_ok": 0, "story_frozen": 0,
        "story_rate_limited": 0, "canonical_session_ready": 0, "missing_canonical_session": 0,
        "general_healthy": 0, "warmup_pending": 0,
    }
    for a in accounts:
        st = (getattr(a, "status", None) or "").value if hasattr(getattr(a, "status", None), "value") else str(getattr(a, "status", "") or "")
        hs = getattr(a, "health_status", None) or ""
        if hs == "alive":
            summary["general_healthy"] += 1
        if hs == "alive" or st == "active":
            summary["active"] += 1
        elif hs == "frozen" or st == "flood_wait":
            summary["frozen"] += 1
        elif hs == "auth_required" or st == "auth_required":
            summary["auth_required"] += 1
        elif hs == "banned" or st == "banned":
            summary["banned"] += 1
        else:
            summary["other"] += 1
        story_avail = get_story_availability(a)
        if story_avail["story_ui_status"] == "ready":
            summary["story_ok"] += 1
        elif story_avail["story_ui_status"] in ("frozen", "restricted"):
            summary["story_frozen"] += 1
        elif story_avail["story_ui_status"] == "rate_limited":
            summary["story_rate_limited"] += 1
        if story_avail["is_story_ready"]:
            summary["story_available"] += 1
        sr = get_session_readiness(a)
        if sr == "canonical_ok":
            summary["canonical_session_ready"] += 1
        elif sr in ("missing_canonical", "needs_reimport"):
            summary["missing_canonical_session"] += 1
        w = format_warmup_for_ui(a)
        if w.get("warmup_status") in ("new", "warming") and w.get("is_warmup_blocked"):
            summary["warmup_pending"] += 1
    return summary


@api.route('/accounts/<int:account_id>', methods=['GET'])
def get_account(account_id):
    """Get account details including story availability from get_story_availability."""
    from src.core.session_paths import account_has_canonical_session, get_story_availability
    with get_db_context() as db:
        account = db.query(Account).filter(Account.id == account_id).first()
        if not account:
            return jsonify({"error": "Account not found"}), 404
        story_avail = get_story_availability(account)
        from src.core.safety_policy import get_story_safety_decision, get_account_risk_level
        decision = get_story_safety_decision(account)
        risk_level = get_account_risk_level(account)
        return jsonify({
            "id": account.id,
            "phone_number": account.phone_number,
            "user_id": account.user_id,
            "username": account.username,
            "first_name": account.first_name,
            "last_name": account.last_name,
            "status": getattr(account.status, "value", str(account.status)) if account.status is not None else "inactive",
            "purpose": getattr(account, "purpose", None) or "both",
            "last_active": _dt_iso_optional(getattr(account, "last_active", None)),
            "last_error": account.last_error,
            "stories_today": account.stories_today,
            "actions_today": account.actions_today,
            "created_at": _dt_iso_optional(getattr(account, "created_at", None)),
            "has_session": account_has_canonical_session(account),
            "session_path": getattr(account, "session_path", None),
            "health_status": getattr(account, "health_status", None),
            "health_reason": getattr(account, "health_reason", None),
            "health_checked_at": _dt_iso_optional(getattr(account, "health_checked_at", None)),
            "story_status": getattr(account, "story_status", None),
            "story_status_reason": getattr(account, "story_status_reason", None),
            "story_status_checked_at": _dt_iso_optional(getattr(account, "story_status_checked_at", None)),
            "story_blocked_until": _dt_iso_optional(getattr(account, "story_blocked_until", None)),
            "story_ui_status": story_avail["story_ui_status"],
            "story_available_label": story_avail["story_available_label"],
            "story_reason": story_avail["story_reason"],
            "is_story_ready": story_avail["is_story_ready"],
            "story_precheck_stale": story_avail.get("story_precheck_stale", False),
            "story_precheck_queue_detail": _story_precheck_queue_detail(account, story_avail.get("story_ui_status")),
            "risk_level": risk_level,
            "story_safety": {
                "allowed": decision.allowed,
                "reason_code": decision.reason_code,
                "human_reason": decision.human_reason,
                "operator_action": decision.operator_action,
                "next_allowed_at": _dt_iso_optional(decision.next_allowed_at),
            },
            "profile_capability_status": getattr(account, "profile_capability_status", None),
            "profile_capability_reason": getattr(account, "profile_capability_reason", None),
            **_identity_fields_for_api(account, None),
        })


@api.route('/accounts/<int:account_id>/story-eligibility', methods=['GET'])
def account_story_eligibility(account_id):
    """Debug/dry-run: return full safety decision for why account is or isn't story-eligible."""
    with get_db_context() as db:
        account = db.query(Account).filter(Account.id == account_id).first()
        if not account:
            return jsonify({"error": "Account not found"}), 404
        from src.core.safety_policy import get_story_safety_decision, get_account_risk_level
        decision = get_story_safety_decision(account)
        risk_level = get_account_risk_level(account)
        return jsonify({
            "account_id": account.id,
            "phone": account.phone_number,
            "risk_level": risk_level,
            "decision": {
                "allowed": decision.allowed,
                "reason_code": decision.reason_code,
                "human_reason": decision.human_reason,
                "operator_action": decision.operator_action,
                "next_allowed_at": _dt_iso_optional(decision.next_allowed_at),
                "precheck_overrode_stale": decision.precheck_overrode_stale,
            },
        })


@api.route('/accounts/check', methods=['POST'])
@admin_api_required
def check_accounts():
    """General health check only: connect, is_user_authorized, get_me. Does NOT verify story publishing. Returns alive/deleted/... + session_valid. Body: update_status, account_ids[], verbose."""
    from src.clients.manager import client_manager
    data = request.get_json() or {}
    update_status = data.get('update_status', False)
    account_ids = data.get('account_ids')  # optional list of ints
    verbose = data.get('verbose', False)
    n_for_timeout = len(account_ids) if isinstance(account_ids, list) and account_ids else 5
    sync_to = _sync_healthcheck_timeout_sec(n_for_timeout)
    try:
        results = run_async_with_timeout(
            client_manager.check_accounts_health(
                update_status=update_status,
                account_ids=account_ids,
                verbose=verbose,
                persist=True,
            ),
            timeout_sec=sync_to,
        )
        _enrich_health_results_with_db_story_state(results)
        return jsonify({
            "success": True,
            "results": results,
            "timeout_sec": sync_to,
            "healthcheck_note": HEALTHCHECK_RESPONSE_NOTE,
        })
    except asyncio.TimeoutError:
        return jsonify({
            "success": False,
            "error": (
                f"Health check timed out after {int(sync_to)}s. "
                "Try fewer accounts or use POST /api/accounts/healthcheck/start for fleet runs."
            ),
            "timeout_sec": sync_to,
        }), 504
    except Exception as e:
        logger.exception("accounts/check failed")
        return jsonify({"success": False, "error": str(e)}), 500


@api.route('/accounts/healthcheck', methods=['POST'])
@admin_api_required
def accounts_healthcheck():
    """Admin-only; throttled. Same as /accounts/check: general health only (connect, auth, get_me). Does NOT test story publishing.
    Requires account_ids. Cap: 10 accounts per sync request (use fleet background for larger runs)."""
    from src.clients.manager import client_manager
    from src.core.models import HealthcheckRun

    data = request.get_json() or {}
    update_status = bool(data.get("update_status", False))
    account_ids = data.get("account_ids")
    verbose = bool(data.get("verbose", False))

    # Require explicit account_ids to prevent unbounded runs (Cloudflare 524)
    if account_ids is None or (isinstance(account_ids, list) and len(account_ids) == 0):
        return jsonify({
            "success": False,
            "error": "account_ids required for sync healthcheck; use limited selection or background job",
        }), 400
    # Cap sync to max 10 accounts to stay within proxy timeout
    _ids = [int(x) for x in account_ids if isinstance(x, (int, str)) and str(x).isdigit()][:10]
    if not _ids:
        return jsonify({
            "success": False,
            "error": "account_ids must contain valid integer IDs",
        }), 400
    account_ids = _ids

    now = datetime.utcnow()
    with get_db_context() as db:
        last = db.query(HealthcheckRun).order_by(HealthcheckRun.started_at.desc()).first()
        if last and last.started_at and (now - last.started_at).total_seconds() < 60:
            retry_after = int(60 - (now - last.started_at).total_seconds())
            return jsonify({
                "success": False,
                "error": f"Throttled: healthcheck was started recently. Retry in ~{retry_after}s.",
                "retry_after_sec": max(1, retry_after),
            }), 429
        run = HealthcheckRun(started_at=now, status="running", requested_by="dashboard")
        db.add(run)
        db.flush()
        run_id = run.id

    out_meta = {}
    sync_to = _sync_healthcheck_timeout_sec(len(account_ids))
    try:
        results = run_async_with_timeout(
            client_manager.check_accounts_health(
                update_status=update_status,
                account_ids=account_ids,
                verbose=verbose,
                persist=True,
                out_meta=out_meta,
            ),
            timeout_sec=sync_to,
        )
        _enrich_health_results_with_db_story_state(results)
        with get_db_context() as db:
            r = db.query(HealthcheckRun).filter(HealthcheckRun.id == run_id).first()
            if r:
                r.finished_at = datetime.utcnow()
                r.status = "success"
                r.summary = f"checked={len(results)}"
        return jsonify({
            "success": True,
            "results": results,
            "run_id": run_id,
            "timeout_sec": sync_to,
            "healthcheck_note": HEALTHCHECK_RESPONSE_NOTE,
            **out_meta,
        })
    except asyncio.TimeoutError:
        with get_db_context() as db:
            r = db.query(HealthcheckRun).filter(HealthcheckRun.id == run_id).first()
            if r:
                r.finished_at = datetime.utcnow()
                r.status = "timeout"
                r.error_message = f"Sync healthcheck timed out after {int(sync_to)}s (limit for this endpoint)."
        return jsonify({
            "success": False,
            "error": (
                f"Healthcheck timed out after {int(sync_to)}s. "
                "Use POST /api/accounts/healthcheck/start for large or slow fleet checks."
            ),
            "run_id": run_id,
            "timeout_sec": sync_to,
        }), 504
    except Exception as e:
        with get_db_context() as db:
            r = db.query(HealthcheckRun).filter(HealthcheckRun.id == run_id).first()
            if r:
                r.finished_at = datetime.utcnow()
                r.status = "error"
                r.summary = str(e)[:500]
        logger.exception("accounts/healthcheck failed")
        return jsonify({"success": False, "error": str(e), "run_id": run_id}), 500


def _healthcheck_remaining_ids(all_ids: List[int], results: list) -> List[int]:
    done = {
        x.get("account_id")
        for x in (results or [])
        if isinstance(x, dict) and x.get("account_id") is not None
    }
    return [i for i in all_ids if i not in done]


def _run_healthcheck_background(
    run_id: int,
    account_ids,
    update_status: bool,
    verbose: bool,
    continued_from: Optional[int] = None,
):
    """
    Background thread: chunked fleet healthcheck. Each chunk runs in its own asyncio event loop
    (via run_async_with_timeout) to match Telethon/rate-limiter loop binding.

    Healthcheck semantics unchanged: connect / is_user_authorized / get_me only (no story publish).
    """
    from src.clients.manager import client_manager
    from src.core.models import HealthcheckRun

    try:
        all_ids = client_manager.healthcheck_eligible_account_ids(account_ids_filter=account_ids)
    except Exception as e:
        logger.exception("healthcheck_eligible_account_ids failed", run_id=run_id)

        def _fail_start():
            with get_db_context() as db:
                r = db.query(HealthcheckRun).filter(HealthcheckRun.id == run_id).first()
                if r:
                    r.finished_at = datetime.utcnow()
                    r.status = "error"
                    r.error_message = str(e)[:500]

        run_with_sqlite_lock_retry(_fail_start, operation="healthcheck_job_error", run_id=run_id)
        return

    grand_total = len(all_ids)
    chunk_size = max(1, HEALTHCHECK_BG_CHUNK_SIZE)
    chunks_est = (grand_total + chunk_size - 1) // chunk_size if grand_total else 0

    scope_note = {
        "healthcheck_scope": "general_telegram_only",
        "healthcheck_description": "connect, is_user_authorized, get_me only; does not test story posting",
        "chunk_size": chunk_size,
        "chunk_timeout_sec": HEALTHCHECK_BG_CHUNK_TIMEOUT_SEC,
        "chunks_estimated": chunks_est,
        "continued_from_job_id": continued_from,
    }

    def _init_progress():
        with get_db_context() as db:
            r0 = db.query(HealthcheckRun).filter(HealthcheckRun.id == run_id).first()
            if r0:
                rem_cap = _healthcheck_remaining_ids(all_ids, [])
                r0.progress = {
                    "checked": 0,
                    "total": grand_total,
                    "remaining": grand_total,
                    "remaining_account_ids": rem_cap[:_HEALTHCHECK_REMAINING_IDS_CAP],
                    "remaining_ids_truncated": grand_total > _HEALTHCHECK_REMAINING_IDS_CAP,
                    "partial": grand_total > 0,
                    **scope_note,
                }
                r0.results = []

    run_with_sqlite_lock_retry(_init_progress, operation="healthcheck_init_progress", run_id=run_id)

    if grand_total == 0:

        def _empty_done():
            with get_db_context() as db:
                r = db.query(HealthcheckRun).filter(HealthcheckRun.id == run_id).first()
                if r:
                    r.finished_at = datetime.utcnow()
                    r.status = "success"
                    r.summary = "no_eligible_session_accounts"
                    r.error_message = None
                    r.progress = {
                        "checked": 0,
                        "total": 0,
                        "remaining": 0,
                        "remaining_account_ids": [],
                        "partial": False,
                        "status_counts": {},
                        **scope_note,
                    }

        run_with_sqlite_lock_retry(_empty_done, operation="healthcheck_job_empty", run_id=run_id)
        logger.info(
            "healthcheck_job_summary",
            run_id=run_id,
            status="success",
            checked=0,
            total=0,
            remaining=0,
            message="no_eligible_session_accounts",
        )
        return

    for chunk_idx, start in enumerate(range(0, grand_total, chunk_size)):
        chunk = all_ids[start : start + chunk_size]

        def _load_base():
            with get_db_context() as db:
                row = db.query(HealthcheckRun).filter(HealthcheckRun.id == run_id).first()
                return len(row.results or []) if row else 0

        base_before = run_with_sqlite_lock_retry(_load_base, operation="healthcheck_chunk_read_base", run_id=run_id)

        logger.info(
            "healthcheck_chunk_start",
            run_id=run_id,
            chunk_index=chunk_idx + 1,
            chunks_estimated=chunks_est,
            chunk_len=len(chunk),
            grand_total=grand_total,
            checked_before_chunk=base_before,
            continued_from_job_id=continued_from,
        )

        def _cb(local_checked: int, _chunk_total: int, result: dict):
            def _write():
                with get_db_context() as db:
                    row = db.query(HealthcheckRun).filter(HealthcheckRun.id == run_id).first()
                    if row:
                        cur = list(row.results or [])
                        cur.append(result)
                        row.results = cur
                        g = base_before + local_checked
                        rem = _healthcheck_remaining_ids(all_ids, cur)
                        row.progress = {
                            "checked": g,
                            "total": grand_total,
                            "remaining": len(rem),
                            "remaining_account_ids": rem[:_HEALTHCHECK_REMAINING_IDS_CAP],
                            "remaining_ids_truncated": len(rem) > _HEALTHCHECK_REMAINING_IDS_CAP,
                            "partial": g < grand_total,
                            "chunk_index": chunk_idx + 1,
                            "chunks_estimated": chunks_est,
                            "status_counts": _healthcheck_status_counts(cur),
                            **scope_note,
                        }

            run_with_sqlite_lock_retry(
                _write,
                operation="healthcheck_progress",
                run_id=run_id,
                chunk_index=chunk_idx + 1,
                account_id=result.get("account_id"),
            )

        t0 = time.monotonic()
        try:
            chunk_results = run_async_with_timeout(
                client_manager.check_accounts_health(
                    update_status=update_status,
                    account_ids=chunk,
                    verbose=verbose,
                    persist=True,
                    out_meta=None,
                    progress_callback=_cb,
                ),
                timeout_sec=HEALTHCHECK_BG_CHUNK_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - t0

            def _timeout_finalize():
                with get_db_context() as db:
                    r = db.query(HealthcheckRun).filter(HealthcheckRun.id == run_id).first()
                    if r:
                        cur_results = list(r.results or [])
                        done_set = {
                            x.get("account_id")
                            for x in cur_results
                            if isinstance(x, dict) and x.get("account_id") is not None
                        }
                        rem = [i for i in all_ids if i not in done_set]
                        r.finished_at = datetime.utcnow()
                        r.status = "timeout"
                        r.error_message = (
                            f"Chunk {chunk_idx + 1}/{chunks_est} timed out after {int(HEALTHCHECK_BG_CHUNK_TIMEOUT_SEC)}s "
                            f"(partial fleet run). Checked {len(done_set)} / {grand_total} accounts; {len(rem)} remaining. "
                            f"POST /api/accounts/healthcheck/start with body {{\"continue_job_id\": {run_id}}} to continue."
                        )
                        r.summary = f"partial_timeout checked={len(done_set)} total={grand_total} remaining={len(rem)}"
                        r.progress = {
                            "checked": len(done_set),
                            "total": grand_total,
                            "remaining": len(rem),
                            "remaining_account_ids": rem[:_HEALTHCHECK_REMAINING_IDS_CAP],
                            "remaining_ids_truncated": len(rem) > _HEALTHCHECK_REMAINING_IDS_CAP,
                            "partial": True,
                            "chunk_timed_out": True,
                            "timed_out_chunk_index": chunk_idx + 1,
                            "chunk_timeout_sec": HEALTHCHECK_BG_CHUNK_TIMEOUT_SEC,
                            "status_counts": _healthcheck_status_counts(cur_results),
                            **scope_note,
                        }
                        return len(done_set), len(rem)
                return 0, 0

            checked_n, rem_n = run_with_sqlite_lock_retry(
                _timeout_finalize, operation="healthcheck_chunk_timeout_persist", run_id=run_id
            )
            logger.warning(
                "healthcheck_chunk_timeout",
                run_id=run_id,
                chunk_index=chunk_idx + 1,
                chunks_estimated=chunks_est,
                chunk_len=len(chunk),
                timeout_sec=HEALTHCHECK_BG_CHUNK_TIMEOUT_SEC,
                elapsed_sec=round(elapsed, 2),
                checked=checked_n,
                total=grand_total,
                remaining=rem_n,
            )
            logger.info(
                "healthcheck_job_summary",
                run_id=run_id,
                status="timeout_partial",
                checked=checked_n,
                total=grand_total,
                remaining=rem_n,
                timed_out_chunk_index=chunk_idx + 1,
            )
            return
        except Exception as e:
            logger.exception("healthcheck background chunk failed", run_id=run_id, chunk_index=chunk_idx)

            def _err_mark():
                with get_db_context() as db:
                    r = db.query(HealthcheckRun).filter(HealthcheckRun.id == run_id).first()
                    if r:
                        r.finished_at = datetime.utcnow()
                        r.status = "error"
                        r.error_message = str(e)[:500]

            run_with_sqlite_lock_retry(_err_mark, operation="healthcheck_chunk_error_persist", run_id=run_id)
            return

        elapsed = time.monotonic() - t0
        logger.info(
            "healthcheck_chunk_done",
            run_id=run_id,
            chunk_index=chunk_idx + 1,
            chunks_estimated=chunks_est,
            chunk_len=len(chunk_results),
            elapsed_sec=round(elapsed, 2),
        )

    def _finalize_ok():
        with get_db_context() as db:
            r = db.query(HealthcheckRun).filter(HealthcheckRun.id == run_id).first()
            if r:
                final = list(r.results or [])
                r.finished_at = datetime.utcnow()
                r.status = "success"
                r.summary = f"checked={len(final)} total={grand_total}"
                r.error_message = None
                r.progress = {
                    "checked": len(final),
                    "total": grand_total,
                    "remaining": 0,
                    "remaining_account_ids": [],
                    "partial": False,
                    "status_counts": _healthcheck_status_counts(final),
                    **scope_note,
                }
                return len(final)
        return 0

    n_done = run_with_sqlite_lock_retry(_finalize_ok, operation="healthcheck_job_finalize", run_id=run_id)
    logger.info(
        "healthcheck_job_summary",
        run_id=run_id,
        status="success",
        checked=n_done,
        total=grand_total,
        remaining=0,
        chunks_estimated=chunks_est,
    )


@api.route('/accounts/healthcheck/start', methods=['POST'])
@admin_api_required
def healthcheck_start():
    """Start background fleet healthcheck. Chunked; poll GET /api/accounts/healthcheck/<job_id>.
    Optional body.continue_job_id: resume from a timed-out/partial job's remaining_account_ids."""
    from src.core.models import HealthcheckRun
    import threading

    data = request.get_json() or {}
    update_status = bool(data.get("update_status", False))
    account_ids = data.get("account_ids")
    verbose = bool(data.get("verbose", False))
    continue_from: Optional[int] = None

    raw_continue = data.get("continue_job_id")
    if raw_continue is not None and str(raw_continue).strip() != "":
        try:
            continue_from = int(raw_continue)
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "continue_job_id must be an integer"}), 400
        with get_db_context() as db:
            prev = db.query(HealthcheckRun).filter(HealthcheckRun.id == continue_from).first()
            if not prev:
                return jsonify({"success": False, "error": f"continue_job_id {continue_from} not found"}), 404
            rem = (prev.progress or {}).get("remaining_account_ids") or []
            if not rem:
                return jsonify({
                    "success": False,
                    "error": "That job has no remaining_account_ids left; start a fresh fleet check.",
                }), 400
            account_ids = [int(x) for x in rem]
    else:
        # account_ids: None/omit = all eligible; non-empty list = those IDs only
        if account_ids is not None and isinstance(account_ids, list):
            account_ids = [int(x) for x in account_ids if isinstance(x, (int, str)) and str(x).isdigit()]
            if not account_ids:
                return jsonify({"success": False, "error": "account_ids must contain valid integer IDs"}), 400

    now = datetime.utcnow()
    with get_db_context() as db:
        last = db.query(HealthcheckRun).order_by(HealthcheckRun.started_at.desc()).first()
        if last and last.started_at and (now - last.started_at).total_seconds() < 60:
            retry_after = int(60 - (now - last.started_at).total_seconds())
            return jsonify({
                "success": False,
                "error": f"Throttled: healthcheck was started recently. Retry in ~{retry_after}s.",
                "retry_after_sec": max(1, retry_after),
            }), 429
        run = HealthcheckRun(
            started_at=now, status="running", requested_by="dashboard",
            results=[], progress={"checked": 0, "total": 0},
        )
        db.add(run)
        db.flush()
        run_id = run.id
        db.commit()

    thread = threading.Thread(
        target=_run_healthcheck_background,
        args=(run_id, account_ids, update_status, verbose, continue_from),
        daemon=True,
    )
    thread.start()

    if continue_from is not None:
        logger.info(
            "healthcheck_continue_start",
            continued_from_job_id=continue_from,
            new_job_id=run_id,
            resume_account_count=len(account_ids) if account_ids else 0,
        )
    else:
        logger.info(
            "healthcheck_start",
            job_id=run_id,
            chunk_size=HEALTHCHECK_BG_CHUNK_SIZE,
            chunk_timeout_sec=HEALTHCHECK_BG_CHUNK_TIMEOUT_SEC,
            explicit_filter=account_ids is not None,
        )

    return jsonify({
        "success": True,
        "job_id": run_id,
        "chunk_size": HEALTHCHECK_BG_CHUNK_SIZE,
        "chunk_timeout_sec": HEALTHCHECK_BG_CHUNK_TIMEOUT_SEC,
        "continued_from_job_id": continue_from,
        "message": (
            "General healthcheck job started (connect/auth/get_me only — not story readiness). "
            "Poll GET /api/accounts/healthcheck/" + str(run_id)
        ),
    }), 201


@api.route('/accounts/healthcheck/<int:job_id>', methods=['GET'])
@admin_api_required
def healthcheck_status(job_id):
    """Get healthcheck job status: totals, partial flag, remaining IDs, status_counts (for curl/UI)."""
    from src.core.models import HealthcheckRun

    with get_db_context() as db:
        r = db.query(HealthcheckRun).filter(HealthcheckRun.id == job_id).first()
        if not r:
            return jsonify({"success": False, "error": "Job not found"}), 404

        prog = dict(r.progress or {})
        results = list(r.results or [])
        checked = prog.get("checked", len(results))
        total = prog.get("total", checked)
        remaining = prog.get("remaining", max(0, int(total) - int(checked)))
        partial = bool(prog.get("partial")) or r.status == "timeout"
        can_continue = partial and remaining > 0 and bool(prog.get("remaining_account_ids"))
        remaining_ids_truncated = bool(prog.get("remaining_ids_truncated"))

        out = {
            "success": True,
            "job_id": job_id,
            "status": r.status,
            "summary": r.summary,
            "partial": partial,
            "can_continue": can_continue,
            "remaining_ids_truncated": remaining_ids_truncated,
            "healthcheck_scope": prog.get("healthcheck_scope", "general_telegram_only"),
            "checked": checked,
            "total": total,
            "remaining": remaining,
            "chunk_size": prog.get("chunk_size"),
            "chunk_index": prog.get("chunk_index"),
            "chunks_estimated": prog.get("chunks_estimated"),
            "chunk_timed_out": bool(prog.get("chunk_timed_out", False)),
            "timed_out_chunk_index": prog.get("timed_out_chunk_index"),
            "chunk_timeout_sec": prog.get("chunk_timeout_sec"),
            "status_counts": prog.get("status_counts") or _healthcheck_status_counts(results),
            "progress": prog,
            "results": results,
            "started_at": r.started_at.isoformat() if r.started_at else None,
        }
        if r.finished_at:
            out["finished_at"] = r.finished_at.isoformat()
        else:
            out["finished_at"] = None
        out["error_message"] = r.error_message
        _enrich_health_results_with_db_story_state(results)
        out["healthcheck_note"] = HEALTHCHECK_RESPONSE_NOTE
        if can_continue:
            hint = (
                f'POST /api/accounts/healthcheck/start with JSON body {{"continue_job_id": {job_id}}} '
                "to check the remaining accounts."
            )
            if remaining_ids_truncated:
                hint += (
                    " Note: `remaining_account_ids` in progress may be capped; `remaining` count is still exact."
                )
            out["continue_hint"] = hint
        elif partial and remaining > 0:
            out["continue_hint"] = (
                "Partial results with accounts still unchecked, but this job has no continuation ID list. "
                "Start a fresh fleet run via POST /api/accounts/healthcheck/start or inspect HealthcheckRun.progress in the DB."
            )
        if "continue_hint" not in out:
            out["continue_hint"] = None

        return jsonify(out)


@api.route('/accounts/<int:account_id>/status', methods=['PUT'])
def update_account_status(account_id):
    """Update account status"""
    data = request.get_json()
    new_status = data.get('status')

    if new_status not in [s.value for s in AccountStatus]:
        return jsonify({"error": "Invalid status"}), 400

    with get_db_context() as db:
        account = db.query(Account).filter(Account.id == account_id).first()
        if not account:
            return jsonify({"error": "Account not found"}), 404

        account.status = AccountStatus(new_status)
        return jsonify({"success": True, "status": account.status.value})


@api.route('/accounts/<int:account_id>/dialogs', methods=['GET'])
def account_dialogs(account_id):
    """Fetch groups/channels the account is in (from Telegram)."""
    from src.clients.manager import client_manager
    limit = request.args.get("limit", 200, type=int)
    try:
        dialogs = run_async(client_manager.get_dialogs(account_id, limit=min(500, limit)))
        return jsonify(dialogs)
    except Exception as e:
        logger.exception("Failed to fetch dialogs")
        return jsonify({"error": str(e)}), 500


@api.route('/accounts/<int:account_id>', methods=['PATCH'])
def update_account(account_id):
    """Update account fields (purpose, manual_review_required). manual_review clearing requires reason and is audited."""
    from src.core.risk_events import record_risk_event, EVENT_MANUAL_REVIEW_CLEARED
    data = request.get_json() or {}
    with get_db_context() as db:
        account = db.query(Account).filter(Account.id == account_id).first()
        if not account:
            return jsonify({"error": "Account not found"}), 404
        if "purpose" in data and data["purpose"] in ("autostory", "messaging", "both"):
            account.purpose = data["purpose"]
        if "manual_review_required" in data and data["manual_review_required"] is False:
            if getattr(account, "manual_review_required", False):
                reason = (data.get("manual_review_clear_reason") or "").strip() or "operator_cleared"
                account.manual_review_required = False
                account.manual_review_reason = None
                record_risk_event(account_id, EVENT_MANUAL_REVIEW_CLEARED, reason)
        return jsonify({"success": True})


@api.route('/accounts/<int:account_id>/set-username', methods=['POST'])
def set_account_username(account_id):
    """Set Telegram username for an account (without @)."""
    from src.clients.manager import client_manager
    data = request.get_json() or {}
    username = data.get("username")
    if not username or not str(username).strip():
        return jsonify({"error": "username required"}), 400
    result = run_async(client_manager.set_account_username(account_id, str(username).strip()))
    if result.get("success"):
        return jsonify(result)
    return jsonify(result), 400


@api.route('/accounts/<int:account_id>/set-profile-photo', methods=['POST'])
def set_account_profile_photo(account_id):
    """Set profile photo for an account. Expects multipart form with 'photo' file."""
    from src.clients.manager import client_manager
    import tempfile
    import os
    if "photo" not in request.files:
        return jsonify({"error": "photo file required"}), 400
    f = request.files["photo"]
    if not f.filename:
        return jsonify({"error": "photo file required"}), 400
    ext = os.path.splitext(f.filename)[1] or ".jpg"
    tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
    try:
        f.save(tmp.name)
        result = run_async(client_manager.set_account_profile_photo(account_id, tmp.name))
        if result.get("success"):
            return jsonify(result)
        return jsonify(result), 400
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


@api.route('/accounts/bulk-set-username/preview', methods=['POST'])
@admin_api_required
def bulk_set_username_preview():
    """Preview bulk username change: eligible accounts and skipped reasons. Uses same eligibility as actual run."""
    from src.core.profile_action_policy import get_profile_action_eligible_accounts
    data = request.get_json() or {}
    prefix = (data.get("username_prefix") or data.get("prefix") or "").strip().replace("@", "").strip()
    if not prefix or len(prefix) < 2:
        return jsonify({"error": "username_prefix required"}), 400
    allow_override = bool(data.get("allow_warming_override", False))
    override_reason = (data.get("override_reason") or "").strip()
    if allow_override and len(override_reason) < 5:
        return jsonify({"error": "override_reason required (min 5 chars) when allow_warming_override=true"}), 400
    eligible, skipped = get_profile_action_eligible_accounts(
        action_type="bulk_username",
        allow_warming_override=allow_override,
        override_reason=override_reason if allow_override else None,
    )
    max_batch = getattr(settings.warmup, "max_bulk_username_batch", 5) or 5
    batch_capped = len(eligible) > max_batch
    would_run = eligible[:max_batch]
    return jsonify({
        "eligible_count": len(eligible),
        "would_run_count": len(would_run),
        "would_run_ids": [a.id for a in would_run],
        "skipped": skipped,
        "batch_capped": batch_capped,
        "max_batch": max_batch,
    })


@api.route('/accounts/bulk-set-username', methods=['POST'])
@admin_api_required
def bulk_set_username():
    """Set Telegram username for accounts. Enforces eligibility (session, cooldown, warming, manual_review). Body: username_prefix, allow_warming_override?, override_reason?"""
    import time
    import random
    from src.clients.manager import client_manager
    from src.core.profile_action_policy import get_profile_action_eligible_accounts
    from src.core.warmup import get_safe_jitter_sec
    from src.core.risk_events import record_risk_event, count_events_last_hour, count_bulk_profile_actions_last_hour, EVENT_USERNAME_CHANGED, EVENT_PROFILE_ACTION_OVERRIDE
    data = request.get_json() or {}
    # Combined bulk profile actions cap (username + photo)
    max_combined = int(getattr(settings.warmup, "max_bulk_profile_actions_per_hour", 8) or 8)
    combined_recent = count_bulk_profile_actions_last_hour()
    if combined_recent >= max_combined:
        return jsonify({
            "error": f"Bulk profile actions cap reached: {combined_recent} in last hour. Max {max_combined}. Retry later.",
            "recent_count": combined_recent,
            "max_per_hour": max_combined,
        }), 429
    prefix = (data.get("username_prefix") or data.get("prefix") or "").strip().replace("@", "").strip()
    if not prefix or len(prefix) < 2:
        return jsonify({"error": "username_prefix required (e.g. mybrand -> mybrand_1, mybrand_2, ...)"}), 400
    allow_override = bool(data.get("allow_warming_override", False))
    override_reason = (data.get("override_reason") or "").strip()
    if allow_override and len(override_reason) < 5:
        return jsonify({"error": "override_reason required (min 5 chars) when allow_warming_override=true"}), 400
    max_per_hour = getattr(settings.warmup, "max_username_changes_per_hour", 5) or 5
    max_batch = getattr(settings.warmup, "max_bulk_username_batch", 5) or 5
    recent = count_events_last_hour(EVENT_USERNAME_CHANGED)
    if recent >= max_per_hour:
        return jsonify({
            "error": f"Hourly cap reached: {recent} username changes in last hour. Max {max_per_hour}. Retry later.",
            "recent_count": recent,
            "max_per_hour": max_per_hour,
        }), 429
    eligible, skipped = get_profile_action_eligible_accounts(
        action_type="bulk_username",
        allow_warming_override=allow_override,
        override_reason=override_reason if allow_override else None,
    )
    if not eligible:
        return jsonify({
            "error": "No eligible accounts",
            "skipped": skipped,
        }), 400
    batch_capped = len(eligible) > max_batch
    accounts = eligible[:max_batch]
    jmin, jmax = get_safe_jitter_sec()
    results = []
    for i, acc in enumerate(accounts):
        if i > 0:
            time.sleep(random.uniform(jmin, jmax))
        suffix = str(i + 1)
        max_prefix_len = 32 - len(suffix) - 1
        p = prefix[:max_prefix_len] if len(prefix) > max_prefix_len else prefix
        username = f"{p}_{suffix}"
        if len(username) < 5:
            username = (prefix + suffix)[:32]
        r = run_async(client_manager.set_account_username(acc.id, username))
        ok_ = r.get("success")
        results.append({"account_id": acc.id, "username": username, "success": ok_, "error": r.get("error")})
        if ok_:
            record_risk_event(acc.id, EVENT_USERNAME_CHANGED, f"bulk:{username}")
            if getattr(acc, "_profile_override_used", False):
                record_risk_event(acc.id, EVENT_PROFILE_ACTION_OVERRIDE, f"bulk_username:{override_reason[:200]}")
    ok = sum(1 for x in results if x["success"])
    return jsonify({
        "success": True,
        "updated": ok,
        "total": len(results),
        "results": results,
        "batch_capped": batch_capped,
        "skipped_count": len(skipped),
        "skipped_sample": skipped[:10],
    })


@api.route('/accounts/bulk-set-profile-photo/preview', methods=['POST'])
@admin_api_required
def bulk_set_profile_photo_preview():
    """Preview bulk profile photo change: eligible accounts and skipped reasons. Uses same eligibility as actual run."""
    from src.core.profile_action_policy import get_profile_action_eligible_accounts
    data = request.get_json() or {}
    allow_override = bool(data.get("allow_warming_override", False))
    override_reason = (data.get("override_reason") or "").strip()
    if allow_override and len(override_reason) < 5:
        return jsonify({"error": "override_reason required (min 5 chars) when allow_warming_override=true"}), 400
    eligible, skipped = get_profile_action_eligible_accounts(
        action_type="bulk_photo",
        allow_warming_override=allow_override,
        override_reason=override_reason if allow_override else None,
    )
    max_batch = getattr(settings.warmup, "max_bulk_photo_batch", 3) or 3
    batch_capped = len(eligible) > max_batch
    would_run = eligible[:max_batch]
    return jsonify({
        "eligible_count": len(eligible),
        "would_run_count": len(would_run),
        "would_run_ids": [a.id for a in would_run],
        "skipped": skipped,
        "batch_capped": batch_capped,
        "max_batch": max_batch,
    })


@api.route('/accounts/bulk-set-profile-photo', methods=['POST'])
@admin_api_required
def bulk_set_profile_photo():
    """Set profile photo for eligible accounts. Uses profile_action_policy (session, cooldown, warming, manual_review)."""
    import tempfile
    import os
    import time
    import random
    from src.clients.manager import client_manager
    from src.core.profile_action_policy import get_profile_action_eligible_accounts
    from src.core.warmup import get_safe_jitter_sec
    from src.core.risk_events import record_risk_event, count_events_last_hour, count_bulk_profile_actions_last_hour, EVENT_PROFILE_PHOTO_CHANGED, EVENT_PROFILE_ACTION_OVERRIDE
    data = request.form.to_dict() if request.form else {}
    # Combined bulk profile actions cap (username + photo)
    max_combined = int(getattr(settings.warmup, "max_bulk_profile_actions_per_hour", 8) or 8)
    combined_recent = count_bulk_profile_actions_last_hour()
    if combined_recent >= max_combined:
        return jsonify({
            "error": f"Bulk profile actions cap reached: {combined_recent} in last hour. Max {max_combined}. Retry later.",
            "recent_count": combined_recent,
            "max_per_hour": max_combined,
        }), 429
    allow_override = bool(data.get("allow_warming_override") in ("true", "1", "yes"))
    override_reason = (data.get("override_reason") or "").strip()
    if allow_override and len(override_reason) < 5:
        return jsonify({"error": "override_reason required (min 5 chars) when allow_warming_override=true"}), 400
    if "photo" not in request.files:
        return jsonify({"error": "photo file required"}), 400
    f = request.files["photo"]
    if not f.filename:
        return jsonify({"error": "photo file required"}), 400
    max_per_hour = getattr(settings.warmup, "max_profile_photo_changes_per_hour", 3) or 3
    max_batch = getattr(settings.warmup, "max_bulk_photo_batch", 3) or 3
    recent = count_events_last_hour(EVENT_PROFILE_PHOTO_CHANGED)
    if recent >= max_per_hour:
        return jsonify({
            "error": f"Hourly cap reached: {recent} profile photo changes in last hour. Max {max_per_hour}. Retry later.",
            "recent_count": recent,
            "max_per_hour": max_per_hour,
        }), 429
    eligible, skipped = get_profile_action_eligible_accounts(
        action_type="bulk_photo",
        allow_warming_override=allow_override,
        override_reason=override_reason if allow_override else None,
    )
    if not eligible:
        return jsonify({
            "error": "No eligible accounts",
            "skipped": skipped,
        }), 400
    batch_capped = len(eligible) > max_batch
    accounts = eligible[:max_batch]
    ext = os.path.splitext(f.filename)[1] or ".jpg"
    tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
    try:
        f.save(tmp.name)
        jmin, jmax = get_safe_jitter_sec()
        results = []
        for i, acc in enumerate(accounts):
            if i > 0:
                time.sleep(random.uniform(jmin, jmax))
            r = run_async(client_manager.set_account_profile_photo(acc.id, tmp.name))
            ok_ = r.get("success")
            results.append({"account_id": acc.id, "success": ok_, "error": r.get("error")})
            if ok_:
                record_risk_event(acc.id, EVENT_PROFILE_PHOTO_CHANGED, "bulk")
                if getattr(acc, "_profile_override_used", False):
                    record_risk_event(acc.id, EVENT_PROFILE_ACTION_OVERRIDE, f"bulk_photo:{override_reason[:200]}")
        ok = sum(1 for x in results if x["success"])
        return jsonify({
            "success": True,
            "updated": ok,
            "total": len(results),
            "results": results,
            "batch_capped": batch_capped,
            "skipped_count": len(skipped),
            "skipped_sample": skipped[:10],
        })
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


def _purge_account_dependencies(db, account_id: int) -> None:
    """
    Delete or unlink all DB rows referencing accounts.id before removing the account.
    Covers scheduler (bindings, jobs, deliveries, templates, profiles, rules), stories, tasks,
    QR tokens, and risk-event log. Required when SQLite foreign_keys are ON or for FK hygiene.
    """
    from sqlalchemy import text

    from src.core.models import QrLoginToken, Story, Task
    from src.core.scheduler_models import (
        AccountTargetBinding,
        MessageDelivery,
        MessageTemplate,
        ScheduledJob,
        ScheduleProfile,
        ScheduleRule,
    )

    db.query(MessageDelivery).filter(MessageDelivery.account_id == account_id).delete(synchronize_session=False)
    db.query(ScheduledJob).filter(ScheduledJob.account_id == account_id).delete(synchronize_session=False)
    db.query(ScheduleRule).filter(ScheduleRule.account_id == account_id).delete(synchronize_session=False)
    db.query(ScheduleProfile).filter(ScheduleProfile.account_id == account_id).delete(synchronize_session=False)

    bind_ids = [
        row[0]
        for row in db.query(AccountTargetBinding.id).filter(AccountTargetBinding.account_id == account_id).all()
    ]
    if bind_ids:
        db.query(MessageTemplate).filter(MessageTemplate.binding_id.in_(bind_ids)).delete(synchronize_session=False)
    db.query(AccountTargetBinding).filter(AccountTargetBinding.account_id == account_id).delete(synchronize_session=False)
    db.query(MessageTemplate).filter(MessageTemplate.account_id == account_id).delete(synchronize_session=False)

    db.query(Story).filter(Story.account_id == account_id).delete(synchronize_session=False)
    db.query(Task).filter(Task.account_id == account_id).update({"account_id": None}, synchronize_session=False)
    db.query(QrLoginToken).filter(QrLoginToken.account_id == account_id).delete(synchronize_session=False)

    try:
        db.execute(text("DELETE FROM account_risk_events WHERE account_id = :aid"), {"aid": account_id})
    except Exception:
        pass


@api.route('/accounts/bulk-delete', methods=['POST'])
@admin_api_required
def bulk_delete_accounts():
    """Delete multiple accounts by id. Body: { account_ids: [1,2,3] }. Removes DB row and canonical session file for each."""
    from src.clients.manager import client_manager
    from src.core.session_paths import get_canonical_session_path

    data = request.get_json() or {}
    account_ids = data.get("account_ids")
    if not account_ids or not isinstance(account_ids, list):
        return jsonify({"error": "account_ids array required"}), 400
    account_ids = [int(x) for x in account_ids if isinstance(x, (int, str)) and str(x).isdigit()]
    if not account_ids:
        return jsonify({"error": "No valid account ids"}), 400

    deleted = []
    errors = []
    for account_id in account_ids:
        try:
            with get_db_context() as db:
                account = db.query(Account).filter(Account.id == account_id).first()
                if not account:
                    errors.append({"id": account_id, "error": "Not found"})
                    continue
                phone = account.phone_number
                _purge_account_dependencies(db, account_id)
                db.delete(account)
                db.commit()
            try:
                run_async(client_manager.remove_account(account_id))
            except Exception as e:
                logger.warning("remove_account after bulk delete", account_id=account_id, error=str(e))
            try:
                path = get_canonical_session_path(account_id)
                if path.is_file():
                    path.unlink()
                    logger.info("Removed canonical session file", account_id=account_id, path=str(path))
            except Exception as e:
                logger.warning("Could not remove session file", account_id=account_id, error=str(e))
            deleted.append({"id": account_id, "phone": phone})
        except Exception as e:
            errors.append({"id": account_id, "error": str(e)})
    return jsonify({
        "success": True,
        "deleted": deleted,
        "errors": errors,
        "message": f"Deleted {len(deleted)} account(s)" + (f"; {len(errors)} error(s)" if errors else ""),
    })


@api.route('/accounts/<int:account_id>', methods=['DELETE'])
def delete_account(account_id):
    """Permanently remove account from DB and from client manager. Removes canonical session file if present."""
    from src.clients.manager import client_manager
    from src.core.session_paths import get_canonical_session_path
    with get_db_context() as db:
        account = db.query(Account).filter(Account.id == account_id).first()
        if not account:
            return jsonify({"error": "Account not found"}), 404
        phone = account.phone_number
        _purge_account_dependencies(db, account_id)
        db.delete(account)
        db.commit()
    try:
        run_async(client_manager.remove_account(account_id))
    except Exception as e:
        logger.warning("remove_account after delete", account_id=account_id, error=str(e))
    # Remove canonical session file if it exists
    try:
        path = get_canonical_session_path(account_id)
        if path.is_file():
            path.unlink()
            logger.info("Removed canonical session file", account_id=account_id, path=str(path))
    except Exception as e:
        logger.warning("Could not remove session file", account_id=account_id, error=str(e))
    return jsonify({"success": True, "message": f"Account {account_id} ({phone}) deleted"})


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
    if result.get("success") and result.get("account_id"):
        try:
            from src.scheduler.provisioning import ensure_scheduler_defaults
            with get_db_context() as db:
                ensure_scheduler_defaults(result["account_id"], db)
        except Exception as e:
            logger.warning("provisioning after complete_auth failed: %s", e)
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
        result = run_async(client_manager.import_session_string(session_string, import_source="paste"))
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    if result.get("success") and result.get("account_id"):
        try:
            from src.scheduler.provisioning import ensure_scheduler_defaults
            with get_db_context() as db:
                ensure_scheduler_defaults(result["account_id"], db)
        except Exception as e:
            logger.warning("provisioning after import_session failed: %s", e)
    if result.get("success"):
        return jsonify(result)
    return jsonify(result), 400


@api.route('/accounts/inspect-tdata', methods=['POST'])
def inspect_tdata():
    """
    Inspect a tdata zip without importing. Returns what we would discover:
    zip structure (folders / inner zips), candidate count and sources, failed_tdata (conversion errors).
    Use this to verify your zip has the expected folders and see which ones fail conversion.
    """
    import tempfile
    import shutil
    from pathlib import Path
    from src.core.tdata_convert import find_tdata_root, find_all_session_strings_in_extracted, tdata_to_session_string
    from src.core.tdata_import import safe_extract_zip, discover_candidates

    file = request.files.get("file")
    passcode = (request.form.get("passcode") or "").strip() or None
    if not file or not file.filename:
        return jsonify({"success": False, "error": "No file uploaded."}), 400
    if not file.filename.lower().endswith(".zip"):
        return jsonify({"success": False, "error": "File must be a .zip archive."}), 400
    file.seek(0, 2)
    size = file.tell()
    file.seek(0)
    if size > 50 * 1024 * 1024:
        return jsonify({"success": False, "error": "Zip file too large (max 50 MB)."}), 400

    tmpdir = None
    try:
        tmpdir = tempfile.mkdtemp(prefix="autostory_inspect_")
        zip_path = Path(tmpdir) / "upload.zip"
        file.save(str(zip_path))
        _, _, extract_err = safe_extract_zip(zip_path, Path(tmpdir))
        if extract_err:
            return jsonify({"success": False, "error": f"Extract failed: {extract_err}"}), 400

        def sync_tdata_to_session(path: str, pwd: Optional[str]):
            return run_async(tdata_to_session_string(path, passcode=pwd))

        candidates, debug = discover_candidates(
            Path(tmpdir),
            passcode,
            sync_tdata_to_session,
            find_tdata_root,
            find_all_session_strings_in_extracted,
        )

        out = {
            "success": True,
            "message": "Inspect only (no accounts imported).",
            "candidate_count": len(candidates),
            "candidates": [{"index": c.get("index"), "source": c.get("source")} for c in candidates],
        }
        if debug.get("top_level_folders") is not None:
            out["zip_folders"] = debug["top_level_folders"]
            out["zip_folder_count"] = len(debug["top_level_folders"])
        if debug.get("inner_zip_count") is not None:
            out["zip_folder_count"] = debug["inner_zip_count"]
            out["zip_folder_names"] = debug.get("inner_zip_names", [])
        if debug.get("failed_tdata"):
            out["failed_tdata"] = [{"source": f.get("source"), "error": f.get("error")} for f in debug["failed_tdata"]]
        if debug.get("first_folder_contents") is not None:
            out["first_folder_contents"] = debug["first_folder_contents"]
            out["first_folder_name"] = (debug.get("top_level_folders") or ["?"])[0]
        return jsonify(out)
    finally:
        if tmpdir:
            try:
                shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception:
                pass


@api.route('/accounts/import-tdata', methods=['POST'])
def import_tdata():
    """
    Import account(s) by uploading a zip of tdata folder(s).
    Uses safe extraction (zip-slip protection, size limits), discovers all candidates,
    imports each with per-account error handling, and returns a detailed report.
    """
    import tempfile
    import shutil
    from pathlib import Path
    from src.clients.manager import client_manager
    from src.core.tdata_convert import find_tdata_root, find_all_session_strings_in_extracted, tdata_to_session_string
    from src.core.tdata_import import safe_extract_zip, discover_candidates

    file = request.files.get("file")
    passcode = (request.form.get("passcode") or "").strip() or None
    # If "passcode" looks like a Telethon session string, use it directly (no file needed)
    import re
    if passcode:
        clean = re.sub(r"\s+", "", passcode)
        if len(clean) >= 90 and re.match(r"^1[A-Za-z0-9+/=]+$", clean):
            try:
                result = run_async(client_manager.import_session_string(clean, import_source="paste"))
                if result.get("success"):
                    if result.get("account_id"):
                        try:
                            from src.scheduler.provisioning import ensure_scheduler_defaults
                            with get_db_context() as db:
                                ensure_scheduler_defaults(result["account_id"], db)
                        except Exception as e:
                            logger.warning("provisioning after import_tdata (paste) failed: %s", e)
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
        tmpdir = tempfile.mkdtemp(prefix="autostory_tdata_")
        zip_path = Path(tmpdir) / "upload.zip"
        file.save(str(zip_path))

        # Single extraction pass for main zip (safe, with limits)
        _, _, extract_err = safe_extract_zip(zip_path, Path(tmpdir))
        if extract_err:
            return jsonify({"success": False, "error": f"Extract failed: {extract_err}"}), 400

        def sync_tdata_to_session(path: str, pwd: Optional[str]):
            return run_async(tdata_to_session_string(path, passcode=pwd))

        # Discovers candidates: each top-level folder (e.g. 14237076181) is a base; tdata is detected by dir name (no map.json required). Restart app after code changes.
        candidates, debug = discover_candidates(
            Path(tmpdir),
            passcode,
            sync_tdata_to_session,
            find_tdata_root,
            find_all_session_strings_in_extracted,
        )

        logger.info("import_tdata: discovered %d candidate(s)", len(candidates))

        if not candidates:
            failed = debug.get("failed_tdata") or []
            if failed:
                # TDATA was found but conversion failed for all; never say "No tdata folder"
                lines = ["TDATA found but conversion failed for all accounts:"]
                for item in failed:
                    lines.append("  %s: TDATA found at %s but conversion failed: %s" % (
                        item.get("source", "?"),
                        item.get("tdata_path", "?"),
                        item.get("error", "Unknown error"),
                    ))
                return jsonify({
                    "success": False,
                    "error": "\n".join(lines),
                    "failed_tdata": failed,
                }), 400
            hint = ""
            try:
                find_tdata_root(Path(tmpdir))
            except ValueError as e:
                hint = " " + str(e)
            if debug.get("first_folder_contents") is not None:
                first_name = (debug.get("top_level_folders") or ["?"])[0]
                hint += " First folder (%s) contents: %s." % (first_name, debug["first_folder_contents"])
            return jsonify({
                "success": False,
                "error": (
                    "No tdata folder (with map.json) and no session string or .session file found in the zip."
                    + hint + " "
                    "You can paste a session string in the 'Session string' box above. "
                    "To get a string from .session files, run: python scripts/session_to_string.py /path/to/session"
                ),
            }), 400

        imported = 0
        updated = 0
        results: list[dict] = []
        for c in candidates:
            idx = c.get("index", len(results) + 1)
            source = c.get("source", "?")
            session_string = c.get("session_string", "")
            ts = datetime.utcnow().isoformat() + "Z"
            try:
                result = run_async(client_manager.import_session_string(session_string, import_source="tdata_zip"))
                if result.get("success"):
                    aid = result.get("account_id")
                    if aid:
                        try:
                            from src.scheduler.provisioning import ensure_scheduler_defaults
                            with get_db_context() as db:
                                ensure_scheduler_defaults(aid, db)
                        except Exception as prov_e:
                            logger.warning("provisioning after import_tdata [%s] failed: %s", source, prov_e)
                    if "Session updated" in (result.get("message") or ""):
                        updated += 1
                        results.append({
                            "index": idx,
                            "source": source,
                            "status": "ok",
                            "message": "updated",
                            "account_id": result.get("account_id"),
                            "user_id": result.get("user_id"),
                            "phone_number": result.get("phone_number"),
                            "username": result.get("username"),
                            "first_name": result.get("first_name"),
                            "last_name": result.get("last_name"),
                            "timestamp": ts,
                            "session_file_saved": result.get("session_file_saved", True),
                            "session_file_path": result.get("session_file_path"),
                        })
                    else:
                        imported += 1
                        results.append({
                            "index": idx,
                            "source": source,
                            "status": "ok",
                            "message": "imported",
                            "account_id": result.get("account_id"),
                            "user_id": result.get("user_id"),
                            "phone_number": result.get("phone_number"),
                            "username": result.get("username"),
                            "first_name": result.get("first_name"),
                            "last_name": result.get("last_name"),
                            "timestamp": ts,
                            "session_file_saved": result.get("session_file_saved", True),
                            "session_file_path": result.get("session_file_path"),
                        })
                else:
                    err_msg = result.get("error", "Unknown error")
                    results.append({
                        "index": idx,
                        "source": source,
                        "status": "failed",
                        "error_type": "import_failed",
                        "error": err_msg,
                        "timestamp": ts,
                    })
                    logger.warning("import_tdata: [%s] failed: %s", source, err_msg)
            except Exception as e:
                err_msg = str(e)
                error_type = type(e).__name__
                results.append({
                    "index": idx,
                    "source": source,
                    "status": "failed",
                    "error_type": error_type,
                    "error": err_msg,
                    "timestamp": ts,
                })
                logger.warning("import_tdata: [%s] exception: %s", source, err_msg, exc_info=True)

        failed = sum(1 for r in results if r.get("status") == "failed")
        session_file_failures = sum(1 for r in results if r.get("status") == "ok" and r.get("session_file_saved") is False)
        logger.info("import_tdata: imported=%d updated=%d failed=%d session_file_failures=%d total=%d",
                    imported, updated, failed, session_file_failures, len(candidates))

        # Single account: preserve previous API shape for backward compatibility
        if len(candidates) == 1:
            r = results[0]
            if r.get("status") == "ok":
                return jsonify({
                    "success": True,
                    "message": r.get("message", "imported"),
                    "account_id": r.get("account_id"),
                    "user_id": r.get("user_id"),
                    "phone_number": r.get("phone_number"),
                    "username": r.get("username"),
                    "first_name": r.get("first_name"),
                    "last_name": r.get("last_name"),
                    "total_discovered": 1,
                    "results": results,
                    "session_file_saved": r.get("session_file_saved", True),
                    "session_file_path": r.get("session_file_path"),
                })
            return jsonify({
                "success": False,
                "error": r.get("error", "Import failed"),
                "total_discovered": 1,
                "results": results,
            }), 400

        # Multiple accounts: summary + full per-account report
        msg_parts = []
        if imported:
            msg_parts.append(f"{imported} imported")
        if updated:
            msg_parts.append(f"{updated} updated")
        if failed:
            msg_parts.append(f"{failed} failed")
        if session_file_failures:
            msg_parts.append(f"{session_file_failures} session file(s) not saved (check server logs)")
        sessions_dir_hint = None
        if session_file_failures:
            try:
                from src.core.session_paths import get_sessions_dir
                sessions_dir_hint = str(get_sessions_dir())
            except Exception:
                pass
        # Include discovery info so user can see what was in the zip and what failed conversion
        out = {
            "success": True,
            "message": "; ".join(msg_parts) if msg_parts else "Done",
            "imported": imported,
            "updated": updated,
            "failed": failed,
            "session_file_failures": session_file_failures,
            "sessions_dir": sessions_dir_hint,
            "total": len(candidates),
            "total_discovered": len(candidates),
            "results": results,
            "errors": [{"index": r["index"], "error": r.get("error")} for r in results if r.get("status") == "failed"][:50],
        }
        # So user can see: how many folders in zip, which ones failed tdata conversion (session invalid/expired)
        if debug.get("top_level_folders") is not None:
            out["zip_folders"] = debug["top_level_folders"]
            out["zip_folder_count"] = len(debug["top_level_folders"])
        if debug.get("inner_zip_count") is not None:
            out["zip_folder_count"] = debug["inner_zip_count"]
            out["zip_folder_names"] = debug.get("inner_zip_names", [])[:30]
        if debug.get("failed_tdata"):
            out["failed_tdata"] = [{"source": f.get("source"), "error": f.get("error")} for f in debug["failed_tdata"]]
        return jsonify(out)
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
# Stories API
# ============================================
@api.route('/stories/mention-sources', methods=['GET'])
def stories_mention_sources():
    """Return discovery sources (with user counts) and uploaded mention sources.
    Includes both rows with source_chat_username set and rows with only source_chat_id/source_chat_title (legacy)."""
    from sqlalchemy import func
    with get_db_context() as db:
        # Sources that have source_chat_username (e.g. from scanner with @group)
        rows = db.query(
            DiscoveredUser.source_chat_username,
            DiscoveredUser.source_chat_title,
            func.count(DiscoveredUser.id).label("count"),
        ).filter(
            DiscoveredUser.source_chat_username.isnot(None)
        ).group_by(
            DiscoveredUser.source_chat_username,
            DiscoveredUser.source_chat_title,
        ).order_by(
            func.count(DiscoveredUser.id).desc()
        ).all()
        discovery_sources = [
            {"id": r[0], "username": r[0], "title": r[1] or r[0], "count": r[2]}
            for r in rows
        ]
        # Legacy: users with source_chat_id/title but no username (e.g. older scans)
        rows_legacy = db.query(
            DiscoveredUser.source_chat_id,
            DiscoveredUser.source_chat_title,
            func.count(DiscoveredUser.id).label("count"),
        ).filter(
            DiscoveredUser.source_chat_username.is_(None),
            DiscoveredUser.source_chat_id.isnot(None),
        ).group_by(
            DiscoveredUser.source_chat_id,
            DiscoveredUser.source_chat_title,
        ).order_by(
            func.count(DiscoveredUser.id).desc()
        ).limit(50).all()
        for r in rows_legacy:
            sid = "id:" + str(r[0])
            title = (r[1] or "Group " + str(r[0])).strip()
            discovery_sources.append({"id": sid, "username": sid, "title": title, "count": r[2]})
        total = db.query(DiscoveredUser).count()
        uploaded = db.query(UploadedMentionSource).order_by(UploadedMentionSource.created_at.desc()).all()
        entry_counts = dict(db.query(UploadedMentionEntry.source_id, func.count(UploadedMentionEntry.id)).group_by(UploadedMentionEntry.source_id).all())
        uploaded_list = [{"id": u.id, "name": u.name, "count": entry_counts.get(u.id, 0)} for u in uploaded]
    return jsonify({
        "discovery_sources": discovery_sources,
        "discovered_total": total,
        "uploaded_sources": uploaded_list,
    })


def _make_json_serializable(obj):
    """Convert datetime and other non-JSON types to serializable form."""
    if obj is None:
        return None
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _make_json_serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_make_json_serializable(v) for v in obj]
    return obj


@api.route('/stories/eligible-accounts', methods=['GET'])
def stories_eligible_accounts():
    """Return list of story-eligible accounts and exclusion reasons summary. Always returns valid JSON."""
    try:
        from src.stories.batch_helpers import get_story_eligible_accounts_for_batch
        eligible, skipped = get_story_eligible_accounts_for_batch(
            only_alive=True,
            skip_flood_wait=True,
            purpose_filter="both",
            daily_cap_per_account=None,
            max_accounts=100,
            requested_account_ids=None,
        )
        # Sanitize eligible: datetime objects cause JSON serialization to fail
        eligible_clean = [_make_json_serializable(e) for e in eligible]

        # Build exclusion_reasons summary
        exclusion_reasons = {}
        for s in skipped:
            r = (s.get("reason") or "other").split("=")[0]
            if r and r != "purpose":
                exclusion_reasons[r] = exclusion_reasons.get(r, 0) + 1
        reason_labels = {
            "no_session": "reimport needed",
            "auth_required": "auth required",
            "story_rate_limited": "rate limited",
            "story_frozen": "frozen",
            "story_restricted": "frozen",
            "story_cooldown": "cooldown",
            "story_precheck_failed": "precheck",
            "warmup_pending": "warmup",
            "manual_review_required": "manual review",
            "risk_level_blocked": "risk",
            "daily_cap": "daily cap reached",
        }
        exclusion_summary = [
            {"reason": reason_labels.get(k, k), "count": v}
            for k, v in sorted(exclusion_reasons.items(), key=lambda x: -x[1])
        ]
        return jsonify({
            "accounts": eligible_clean,
            "exclusion_reasons": exclusion_summary,
        })
    except Exception as e:
        logger.exception("stories_eligible_accounts failed")
        return jsonify({
            "accounts": [],
            "exclusion_reasons": [],
            "error": "Failed to load eligible accounts",
            "detail": str(e),
        }), 500


@api.route('/stories/preview-batch', methods=['POST'])
def preview_batch():
    """Preview batch: eligible accounts, skipped, mention pool size, sample mentions, warnings."""
    from src.stories.batch_helpers import get_story_eligible_accounts_for_batch, get_mention_pool, get_mention_pool_from_sources, apply_pool_behavior
    data = request.get_json() or {}
    max_stories = data.get('max_stories')
    if max_stories is not None:
        max_stories = int(max_stories)
    mentions_per_story = int(data.get('mentions_per_story') or 5)
    source_type = data.get('source_type') or 'discovery'
    source_id = data.get('source_id') or None
    source_sources = data.get('source_sources')
    if source_sources and not isinstance(source_sources, list):
        source_sources = None
    if source_sources:
        source_sources = [x for x in source_sources if isinstance(x, dict)]
    avoid_reuse_days = data.get('avoid_reuse_days')
    if avoid_reuse_days is not None:
        avoid_reuse_days = int(avoid_reuse_days)
    pool_behavior = data.get('pool_behavior') or 'stop_batch'
    only_alive = data.get('only_alive', True)
    skip_flood_wait = data.get('skip_flood_wait', True)
    purpose_filter = data.get('purpose_filter') or 'both'
    daily_cap = data.get('daily_cap_per_account')
    if daily_cap is not None:
        daily_cap = int(daily_cap)
    max_accounts = data.get('max_accounts')
    if max_accounts is not None:
        max_accounts = int(max_accounts)
    # Enforce config cap for preview consistency with run_batch
    try:
        cfg_max = getattr(settings.warmup, "max_accounts_per_story_batch", 10) or 10
        if max_accounts is None:
            max_accounts = cfg_max
        else:
            max_accounts = min(max_accounts, cfg_max)
        # Canary mode: default batch size 1; require canary_batch_ok for more (mirror run_batch)
        canary_batch_ok = data.get("canary_batch_ok") in (True, "true", "1", "yes")
        canary_size = int(getattr(settings.warmup, "canary_default_batch_size", 1) or 1)
        require_canary = bool(getattr(settings.warmup, "canary_batch_ok_required", True))
        if require_canary and not canary_batch_ok:
            max_accounts = min(max_accounts, canary_size)
    except Exception:
        if max_accounts is None:
            max_accounts = 10
    min_accounts = int(data.get('min_accounts') or 0)

    eligible, skipped = get_story_eligible_accounts_for_batch(
        only_alive=only_alive,
        skip_flood_wait=skip_flood_wait,
        purpose_filter=purpose_filter,
        daily_cap_per_account=daily_cap,
        max_accounts=max_accounts,
        requested_account_ids=None,
    )
    requested_account_ids = data.get('account_ids')
    if requested_account_ids is not None and not isinstance(requested_account_ids, list):
        requested_account_ids = [int(requested_account_ids)] if requested_account_ids else []
    if requested_account_ids:
        requested_account_ids = [int(x) for x in requested_account_ids if x is not None]
    if requested_account_ids:
        eligible_ids = {e["id"] for e in eligible}
        selected_ids = [x for x in requested_account_ids if x in eligible_ids]
        eligible = [e for e in eligible if e["id"] in selected_ids]
    if max_stories is None or max_stories <= 0:
        max_stories = len(eligible)
    limit_pool = max_stories * mentions_per_story * 2
    if source_sources:
        pool, total_available = get_mention_pool_from_sources(
            source_sources,
            limit=limit_pool,
            avoid_reuse_days=avoid_reuse_days,
        )
    else:
        pool, total_available = get_mention_pool(
            source_type=source_type,
            source_id=source_id,
            limit=limit_pool,
            avoid_reuse_days=avoid_reuse_days,
        )
    pool, total_available, pool_warnings = apply_pool_behavior(
        pool, total_available, max_stories, mentions_per_story,
        pool_behavior, source_type, source_id, avoid_reuse_days,
    )
    pool_user_ids = [p["user_id"] for p in pool]
    sample = pool[:min(10, len(pool))]
    estimated_stories = min(max_stories, len(eligible), (len(pool_user_ids) // mentions_per_story) if mentions_per_story else max_stories)
    warnings = list(pool_warnings)
    if len(eligible) < min_accounts and min_accounts > 0:
        warnings.append(f"Eligible accounts ({len(eligible)}) below minimum ({min_accounts})")
    if total_available < mentions_per_story * max_stories and pool_behavior != "stop_batch":
        warnings.append(f"Mention pool ({total_available}) may be small for {max_stories} stories × {mentions_per_story} mentions")
    if not pool_user_ids and estimated_stories > 0 and pool_behavior == "stop_batch":
        warnings.append("No mention pool: add users or change pool behavior")
    return jsonify({
        "eligible_accounts": eligible,
        "skipped_accounts": skipped,
        "mention_pool_size": len(pool_user_ids),
        "mention_pool_total_available": total_available,
        "sample_mentions": sample,
        "estimated_stories": estimated_stories,
        "warnings": warnings,
    })


@api.route('/stories/batch-history', methods=['GET'])
def stories_batch_history():
    """Last N batch runs for analytics."""
    from src.core.models import StoryBatchRun
    limit = min(50, max(1, request.args.get('limit', 20, type=int)))
    with get_db_context() as db:
        runs = db.query(StoryBatchRun).order_by(StoryBatchRun.started_at.desc()).limit(limit).all()
        return jsonify({
            "runs": [
                {
                    "id": r.id,
                    "started_at": r.started_at.isoformat() if r.started_at else None,
                    "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                    "config": r.config,
                    "total_attempted": r.total_attempted or 0,
                    "successful": r.successful or 0,
                    "failed": r.failed or 0,
                    "skipped_count": r.skipped_count or 0,
                    "errors_json": r.errors_json,
                    "mention_pool_size": r.mention_pool_size,
                }
                for r in runs
            ]
        })


@api.route('/stories/templates', methods=['GET'])
def list_story_templates():
    """List all story templates."""
    with get_db_context() as db:
        templates = db.query(StoryTemplate).order_by(StoryTemplate.updated_at.desc()).all()
        return jsonify([
            {
                "id": t.id,
                "name": t.name,
                "caption": t.caption,
                "mentions_per_story": t.mentions_per_story,
                "max_stories": t.max_stories,
                "mention_source_type": t.mention_source_type,
                "mention_source_id": t.mention_source_id,
                "avoid_reuse_days": t.avoid_reuse_days,
                "pool_behavior": t.pool_behavior,
                "only_alive": t.only_alive,
                "skip_flood_wait": t.skip_flood_wait,
                "purpose_filter": t.purpose_filter,
                "max_accounts": t.max_accounts,
                "daily_cap_per_account": t.daily_cap_per_account,
                "unique_mentions_across_batch": t.unique_mentions_across_batch,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "updated_at": t.updated_at.isoformat() if t.updated_at else None,
            }
            for t in templates
        ])


@api.route('/stories/templates', methods=['POST'])
def create_story_template():
    """Create a story template from JSON body."""
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    with get_db_context() as db:
        t = StoryTemplate(
            name=name,
            caption=data.get("caption"),
            mentions_per_story=int(data.get("mentions_per_story") or 5),
            max_stories=int(data.get("max_stories") or 10),
            mention_source_type=data.get("mention_source_type") or "discovery",
            mention_source_id=data.get("mention_source_id"),
            avoid_reuse_days=int(data.get("avoid_reuse_days") or 0),
            pool_behavior=data.get("pool_behavior") or "stop_batch",
            only_alive=data.get("only_alive", True),
            skip_flood_wait=data.get("skip_flood_wait", True),
            purpose_filter=data.get("purpose_filter") or "both",
            max_accounts=int(data["max_accounts"]) if data.get("max_accounts") not in (None, "") else None,
            daily_cap_per_account=int(data["daily_cap_per_account"]) if data.get("daily_cap_per_account") not in (None, "") else None,
            unique_mentions_across_batch=data.get("unique_mentions_across_batch", True),
        )
        db.add(t)
        db.flush()
        return jsonify({"id": t.id, "name": t.name})


@api.route('/stories/templates/<int:template_id>', methods=['DELETE'])
def delete_story_template(template_id):
    """Delete a story template."""
    with get_db_context() as db:
        t = db.query(StoryTemplate).filter(StoryTemplate.id == template_id).first()
        if not t:
            return jsonify({"error": "Not found"}), 404
        db.delete(t)
        return jsonify({"success": True})


@api.route('/stories/blacklist', methods=['GET'])
def list_mention_blacklist():
    """List blacklisted users."""
    with get_db_context() as db:
        rows = db.query(MentionBlacklist).order_by(MentionBlacklist.created_at.desc()).all()
        return jsonify([
            {"id": r.id, "user_id": r.user_id, "username": r.username, "reason": r.reason, "created_at": r.created_at.isoformat() if r.created_at else None}
            for r in rows
        ])


@api.route('/stories/blacklist', methods=['POST'])
def add_mention_blacklist():
    """Add user to blacklist. Body: user_id (optional), username (optional), reason."""
    data = request.get_json() or {}
    user_id = data.get("user_id")
    username = (data.get("username") or "").strip().lstrip("@") or None
    if not user_id and not username:
        return jsonify({"error": "user_id or username required"}), 400
    if user_id is not None:
        user_id = int(user_id)
    with get_db_context() as db:
        existing = db.query(MentionBlacklist).filter(
            (MentionBlacklist.user_id == user_id) if user_id else (MentionBlacklist.username == username)
        ).first()
        if existing:
            return jsonify({"error": "Already blacklisted", "id": existing.id}), 400
        r = MentionBlacklist(user_id=user_id, username=username, reason=(data.get("reason") or "").strip() or None)
        db.add(r)
        db.flush()
        return jsonify({"id": r.id})


@api.route('/stories/blacklist/<int:entry_id>', methods=['DELETE'])
def delete_mention_blacklist(entry_id):
    """Remove user from blacklist."""
    with get_db_context() as db:
        r = db.query(MentionBlacklist).filter(MentionBlacklist.id == entry_id).first()
        if not r:
            return jsonify({"error": "Not found"}), 404
        db.delete(r)
        return jsonify({"success": True})


@api.route('/stories/mention-sources/upload', methods=['POST'])
def upload_mention_source():
    """Upload a .txt file with usernames or user IDs (one per line). Creates a new uploaded mention source."""
    from pathlib import Path
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "No file uploaded"}), 400
    if not file.filename.lower().endswith(".txt"):
        return jsonify({"error": "Only .txt files allowed"}), 400
    name = (request.form.get("name") or Path(file.filename).stem or "Uploaded list").strip()[:255]
    lines = [line.strip() for line in file.stream.read().decode("utf-8", errors="ignore").splitlines() if line.strip()]
    with get_db_context() as db:
        source = UploadedMentionSource(name=name)
        db.add(source)
        db.flush()
        for line in lines:
            line = line.strip().lstrip("@")
            if not line:
                continue
            user_id = None
            username = None
            if line.isdigit():
                user_id = int(line)
            else:
                username = line
            db.add(UploadedMentionEntry(source_id=source.id, user_id=user_id, username=username))
        return jsonify({"id": source.id, "name": source.name, "count": len(lines)})


@api.route('/stories/schedules', methods=['GET'])
def list_story_schedules():
    """List story batch schedules."""
    with get_db_context() as db:
        rows = db.query(StorySchedule).order_by(StorySchedule.run_at).all()
        return jsonify([
            {
                "id": r.id,
                "story_template_id": r.story_template_id,
                "name": r.name,
                "run_at": r.run_at.isoformat() if r.run_at else None,
                "repeat": r.repeat,
                "media_path": r.media_path,
                "max_accounts": r.max_accounts,
                "is_enabled": r.is_enabled,
                "last_run_at": r.last_run_at.isoformat() if r.last_run_at else None,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ])


@api.route('/stories/schedules', methods=['POST'])
def create_story_schedule():
    """Create a story schedule. Body: story_template_id, run_at (ISO), name (optional), media_path (optional), max_accounts (optional), repeat (optional: daily, weekly)."""
    data = request.get_json() or {}
    template_id = data.get("story_template_id")
    if not template_id:
        return jsonify({"error": "story_template_id required"}), 400
    run_at_str = data.get("run_at")
    if not run_at_str:
        return jsonify({"error": "run_at required"}), 400
    try:
        run_at = datetime.fromisoformat(run_at_str.replace("Z", "+00:00"))
    except Exception:
        return jsonify({"error": "run_at must be ISO datetime"}), 400
    with get_db_context() as db:
        t = db.query(StoryTemplate).filter(StoryTemplate.id == int(template_id)).first()
        if not t:
            return jsonify({"error": "Template not found"}), 404
        s = StorySchedule(
            story_template_id=t.id,
            name=(data.get("name") or "").strip() or None,
            run_at=run_at,
            repeat=(data.get("repeat") or "").strip() or None,
            media_path=(data.get("media_path") or "").strip() or None,
            max_accounts=int(data["max_accounts"]) if data.get("max_accounts") not in (None, "") else None,
            is_enabled=data.get("is_enabled", True),
        )
        db.add(s)
        db.flush()
        return jsonify({"id": s.id, "run_at": s.run_at.isoformat()})


@api.route('/stories/schedules/<int:schedule_id>', methods=['PATCH'])
def update_story_schedule(schedule_id):
    """Update a story schedule. Body: run_at, is_enabled, media_path, max_accounts, repeat, name."""
    data = request.get_json() or {}
    with get_db_context() as db:
        s = db.query(StorySchedule).filter(StorySchedule.id == schedule_id).first()
        if not s:
            return jsonify({"error": "Not found"}), 404
        if "run_at" in data and data["run_at"]:
            try:
                s.run_at = datetime.fromisoformat(data["run_at"].replace("Z", "+00:00"))
            except Exception:
                pass
        if "is_enabled" in data:
            s.is_enabled = bool(data["is_enabled"])
        if "media_path" in data:
            s.media_path = (data["media_path"] or "").strip() or None
        if "max_accounts" in data:
            s.max_accounts = int(data["max_accounts"]) if data["max_accounts"] not in (None, "") else None
        if "repeat" in data:
            s.repeat = (data["repeat"] or "").strip() or None
        if "name" in data:
            s.name = (data["name"] or "").strip() or None
        return jsonify({"success": True})


@api.route('/stories/schedules/<int:schedule_id>', methods=['DELETE'])
def delete_story_schedule(schedule_id):
    """Delete a story schedule."""
    with get_db_context() as db:
        s = db.query(StorySchedule).filter(StorySchedule.id == schedule_id).first()
        if not s:
            return jsonify({"error": "Not found"}), 404
        db.delete(s)
        return jsonify({"success": True})


@api.route('/stories/upload-media', methods=['POST'])
def upload_story_media():
    """Upload a media file for story publishing (photo or video). Returns path for use in publish."""
    from pathlib import Path
    from config.settings import settings
    import uuid
    file = request.files.get('file')
    if not file or not file.filename:
        return jsonify({"error": "No file uploaded"}), 400
    ext = Path(file.filename).suffix.lower()
    if ext not in ('.jpg', '.jpeg', '.png', '.webp', '.mp4', '.mov', '.avi', '.webm'):
        return jsonify({"error": "Unsupported format. Use JPG, PNG, MP4."}), 400
    media_dir = Path(settings.storage.media_dir)
    media_dir.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}{ext}"
    path = media_dir / name
    try:
        file.save(str(path))
        return jsonify({"success": True, "path": str(path)})
    except Exception as e:
        logger.exception("upload_story_media failed")
        return jsonify({"error": str(e)}), 500


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


@api.route('/stories/<int:story_id>', methods=['GET'])
def get_story(story_id):
    """Get a single story (for editing)."""
    with get_db_context() as db:
        story = db.query(Story).filter(Story.id == story_id).first()
        if not story:
            return jsonify({"error": "Story not found"}), 404
        return jsonify({
            "id": story.id,
            "account_id": story.account_id,
            "caption": story.caption,
            "mentioned_usernames": story.mentioned_usernames or [],
            "mentioned_user_ids": story.mentioned_user_ids or [],
            "views_count": story.views_count,
            "published_at": story.published_at.isoformat() if story.published_at else None,
        })


@api.route('/stories/<int:story_id>', methods=['PATCH'])
def update_story(story_id):
    """Update a story record (caption, etc.). Does not change the story on Telegram."""
    data = request.get_json() or {}
    with get_db_context() as db:
        story = db.query(Story).filter(Story.id == story_id).first()
        if not story:
            return jsonify({"error": "Story not found"}), 404
        if "caption" in data:
            story.caption = data["caption"] if data["caption"] else None
        if "mentioned_usernames" in data:
            story.mentioned_usernames = data["mentioned_usernames"] if isinstance(data["mentioned_usernames"], list) else []
        db.commit()
        return jsonify({
            "id": story.id,
            "caption": story.caption,
            "mentions": len(story.mentioned_user_ids) if story.mentioned_user_ids else 0,
        })


@api.route('/stories/publish', methods=['POST'])
def publish_story():
    """Publish a new story. If mentions not provided, auto-select from discovered users (like the bot). Enforces safety: warmup, cooldown, precheck."""
    from src.stories.publisher import story_publisher
    from src.clients.manager import client_manager
    from src.core.safety_policy import get_story_safety_decision, log_story_exclusion
    data = request.get_json()
    account_id = data.get('account_id')
    media_path = data.get('media_path')
    caption = data.get('caption') or ''
    mentions = data.get('mentions', [])
    mentions_count = int(data.get('mentions_count', 5))
    if not account_id or not media_path:
        return jsonify({"error": "account_id and media_path required"}), 400
    with get_db_context() as db:
        account = db.query(Account).filter(Account.id == account_id).first()
        if not account:
            return jsonify({"error": "Account not found"}), 404
        decision = get_story_safety_decision(account, requested_action="story_publish")
        if not decision.allowed:
            log_story_exclusion(account_id, account.phone_number, "story_publish", decision)
            return jsonify({
                "success": False,
                "error": decision.human_reason,
                "reason_code": decision.reason_code,
                "operator_action": decision.operator_action,
                "next_allowed_at": decision.next_allowed_at.isoformat() if decision.next_allowed_at else None,
            }), 403
    async def publish():
        client = await client_manager.get_client(account_id)
        if not client:
            return {"success": False, "error": "Client not found or not connected"}
        if not mentions:
            from src.core.models import DiscoveredUser
            with get_db_context() as db:
                users = db.query(DiscoveredUser).filter(
                    DiscoveredUser.times_mentioned == 0,
                    DiscoveredUser.username.isnot(None),
                ).order_by(DiscoveredUser.discovered_at.desc()).limit(mentions_count).all()
            mentions_to_use = [u.user_id for u in users]
        else:
            mentions_to_use = mentions
        return await story_publisher.publish_story(
            client_wrapper=client,
            media_path=media_path,
            caption=caption,
            mentions=mentions_to_use,
            campaign_id=data.get('campaign_id')
        )
    result = run_async(publish())
    return jsonify(result)


@api.route('/stories/batch', methods=['POST'])
def publish_batch():
    """Publish stories in batch. Supports source_type, source_id, eligibility and mention strategy options."""
    from src.stories.run_batch import run_batch_async
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "Request body must be JSON"}), 400
    logger.info(
        "Batch publish started",
        media_path=data.get("media_path"),
        source_id=data.get("source_id"),
        content_type=request.content_type,
        payload_keys=list(data.keys()) if data else [],
        account_ids=data.get("account_ids"),
        max_stories=data.get("max_stories"),
        mentions_per_story=data.get("mentions_per_story"),
    )
    try:
        result = run_async(run_batch_async(data))
        logger.info("Batch publish finished", success=result.get("success"), run_id=result.get("run_id"))
        return jsonify(result)
    except Exception as e:
        logger.exception("Batch publish failed")
        return jsonify({"success": False, "error": str(e)}), 500


async def _run_batch_with_config(data: dict):
    """Thin wrapper for run_batch_async (kept for any direct callers)."""
    from src.stories.run_batch import run_batch_async
    return await run_batch_async(data)


# ============================================
# Discovery API
# ============================================
@api.route('/discovery/sources', methods=['GET'])
def list_discovery_sources():
    """List distinct source groups (by username) with user counts for filtering."""
    with get_db_context() as db:
        from sqlalchemy import func
        rows = db.query(
            DiscoveredUser.source_chat_username,
            DiscoveredUser.source_chat_title,
            func.count(DiscoveredUser.id).label("count"),
        ).filter(
            DiscoveredUser.source_chat_username.isnot(None)
        ).group_by(
            DiscoveredUser.source_chat_username,
            DiscoveredUser.source_chat_title,
        ).order_by(
            func.count(DiscoveredUser.id).desc()
        ).all()
        return jsonify([
            {"username": r[0], "title": r[1] or r[0], "count": r[2]}
            for r in rows
        ])


@api.route('/discovery/users', methods=['GET'])
def list_discovered_users():
    """List discovered users. Optional: mentioned, source_username (filter by group @username)."""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    mentioned = request.args.get('mentioned', None)
    source_username = request.args.get('source_username', "").strip().lstrip("@") or None
    search = request.args.get('search', "").strip() or None
    with get_db_context() as db:
        query = db.query(DiscoveredUser)
        if mentioned == 'true':
            query = query.filter(DiscoveredUser.times_mentioned > 0)
        elif mentioned == 'false':
            query = query.filter(DiscoveredUser.times_mentioned == 0)
        if source_username:
            query = query.filter(DiscoveredUser.source_chat_username == source_username)
        if search:
            like = f"%{search}%"
            query = query.filter(
                (DiscoveredUser.username.ilike(like)) | (DiscoveredUser.first_name.ilike(like))
            )
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
                    "source_username": u.source_chat_username,
                    "times_mentioned": u.times_mentioned,
                    "discovered_at": u.discovered_at.isoformat(),
                }
                for u in users
            ]
        })


@api.route('/discovery/scan', methods=['POST'])
def scan_channel():
    """Scan channel(s) for users (group/channel usernames or links, one per line)."""
    from src.discovery.scanner import user_discovery
    data = request.get_json() or {}
    channels_raw = data.get('channels', [])
    if isinstance(channels_raw, str):
        channels_raw = [s.strip() for s in channels_raw.splitlines() if s.strip()]
    channels = [c.strip() for c in channels_raw if c.strip()]
    if not channels:
        return jsonify({"success": False, "error": "channels list required (e.g. @p2pgroup or one per line)"}), 400
    try:
        result = run_async(user_discovery.discover_from_groups(
            group_usernames=channels,
            days_back=365,
        ))
        if not result.get("success") and result.get("group_results"):
            errors = []
            for gr in result["group_results"]:
                if gr.get("errors"):
                    errors.extend(gr["errors"])
            if errors:
                return jsonify({**result, "error": "; ".join(str(e) for e in errors[:5])})
        return jsonify(result)
    except Exception as e:
        logger.exception("discovery/scan failed")
        return jsonify({"success": False, "error": str(e)})


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
    """Create a new campaign"""
    data = request.get_json()
    with get_db_context() as db:
        campaign = Campaign(
            name=data.get('name'),
            description=data.get('description'),
            target_chat_ids=data.get('target_chats', []),
            story_templates=data.get('templates', []),
            media_files=data.get('media_files', []),
        )
        db.add(campaign)
        db.commit()
        db.refresh(campaign)
        return jsonify({"success": True, "campaign_id": campaign.id, "name": campaign.name})


@api.route('/campaigns/<int:campaign_id>', methods=['PUT'])
def update_campaign(campaign_id):
    """Update a campaign"""
    data = request.get_json()
    with get_db_context() as db:
        campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
        if not campaign:
            return jsonify({"error": "Campaign not found"}), 404
        for key in ['name', 'description', 'is_active', 'target_chat_ids', 'story_templates', 'media_files']:
            if key in data:
                setattr(campaign, key, data[key])
        return jsonify({"success": True})


@api.route('/campaigns/<int:campaign_id>', methods=['DELETE'])
def delete_campaign(campaign_id):
    """Delete a campaign"""
    with get_db_context() as db:
        campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
        if not campaign:
            return jsonify({"error": "Campaign not found"}), 404
        db.delete(campaign)
        return jsonify({"success": True})


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


@web.route('/')
@login_required
def index():
    """Dashboard home page"""
    return render_template('index.html')


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
    """Campaigns page"""
    return render_template('campaigns.html')


@web.route('/scheduler')
@login_required
def scheduler_page():
    """Scheduler configuration page"""
    return render_template('scheduler.html')


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
