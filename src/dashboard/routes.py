"""
Dashboard Routes - API and Web endpoints
"""
import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
from functools import wraps
from datetime import datetime
from typing import Optional

def _dt_iso(v):
    """Return ISO string for datetime-like or keep strings as-is."""
    if v is None:
        return None
    iso = getattr(v, "isoformat", None)
    if callable(iso):
        try:
            return iso()
        except Exception:
            pass
    return str(v)


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
    """Run async function in sync context"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


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
@api.route('/accounts', methods=['GET'])
def list_accounts():
    """List all accounts. Supports ?purpose=messaging|autostory to filter."""
    with get_db_context() as db:
        try:
            accounts = db.query(Account).all()
        except Exception as e:
            logger.warning("Accounts query failed (missing purpose column?), falling back", error=str(e))
            from sqlalchemy import text
            rows = db.execute(text("SELECT id, phone_number, username, first_name, status, last_active, stories_today FROM accounts")).fetchall()
            result = []
            for r in rows:
                result.append({
                    "id": r.id,
                    "phone_number": r.phone_number,
                    "username": r.username,
                    "first_name": r.first_name,
                    "status": getattr(r.status, "value", r.status) if r.status is not None else "inactive",
                    "purpose": "both",
                    "last_active": _dt_iso(r.last_active) if r.last_active else None,
                    "stories_today": r.stories_today or 0,
                })
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
            result.append({
                "id": a.id,
                "phone_number": a.phone_number,
                "username": a.username,
                "first_name": a.first_name,
                "status": getattr(a.status, "value", str(a.status)) if a.status is not None else "inactive",
                "purpose": p,
                "last_active": _dt_iso(account.last_active) if a.last_active else None,
                "stories_today": a.stories_today if a.stories_today is not None else 0,
            })
        return jsonify(result)


@api.route('/accounts/<int:account_id>', methods=['GET'])
def get_account(account_id):
    """Get account details"""
    with get_db_context() as db:
        account = db.query(Account).filter(Account.id == account_id).first()
        if not account:
            return jsonify({"error": "Account not found"}), 404

        return jsonify({
            "id": account.id,
            "phone_number": account.phone_number,
            "user_id": account.user_id,
            "username": account.username,
            "first_name": account.first_name,
            "last_name": account.last_name,
            "status": getattr(account.status, "value", str(account.status)) if account.status is not None else "inactive",
            "purpose": getattr(account, "purpose", None) or "both",
            "last_active": _dt_iso(account.last_active) if account.last_active else None,
            "last_error": account.last_error,
            "stories_today": account.stories_today,
            "actions_today": account.actions_today,
            "created_at": _dt_iso(account.created_at) if account.created_at else None,
        })


@api.route('/accounts/check', methods=['POST'])
def check_accounts():
    """Check accounts: connect + API calls, return alive/deleted/... with reason_code. Optional body: update_status, account_ids[], verbose."""
    from src.clients.manager import client_manager
    data = request.get_json() or {}
    update_status = data.get('update_status', False)
    account_ids = data.get('account_ids')  # optional list of ints
    verbose = data.get('verbose', False)
    try:
        results = run_async(client_manager.check_accounts_health(
            update_status=update_status,
            account_ids=account_ids,
            verbose=verbose,
        ))
        return jsonify({"success": True, "results": results})
    except Exception as e:
        logger.exception("accounts/check failed")
        return jsonify({"success": False, "error": str(e)}), 500


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
    """Update account fields (e.g. purpose)"""
    data = request.get_json() or {}
    with get_db_context() as db:
        account = db.query(Account).filter(Account.id == account_id).first()
        if not account:
            return jsonify({"error": "Account not found"}), 404
        if "purpose" in data and data["purpose"] in ("autostory", "messaging", "both"):
            account.purpose = data["purpose"]
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


@api.route('/accounts/bulk-set-username', methods=['POST'])
def bulk_set_username():
    """Set Telegram username for all accounts with session. Body: { \"username_prefix\": \"mybrand\" } -> mybrand_1, mybrand_2, ... (Telegram requires unique usernames)."""
    from src.clients.manager import client_manager
    data = request.get_json() or {}
    prefix = (data.get("username_prefix") or data.get("prefix") or "").strip().replace("@", "").strip()
    if not prefix or len(prefix) < 2:
        return jsonify({"error": "username_prefix required (e.g. mybrand -> mybrand_1, mybrand_2, ...)"}), 400
    with get_db_context() as db:
        accounts = db.query(Account).filter(Account.session_string.isnot(None)).order_by(Account.id).all()
    if not accounts:
        return jsonify({"error": "No accounts with session"}), 400
    results = []
    for i, acc in enumerate(accounts):
        suffix = str(i + 1)
        max_prefix_len = 32 - len(suffix) - 1
        p = prefix[:max_prefix_len] if len(prefix) > max_prefix_len else prefix
        username = f"{p}_{suffix}"
        if len(username) < 5:
            username = (prefix + suffix)[:32]
        r = run_async(client_manager.set_account_username(acc.id, username))
        results.append({"account_id": acc.id, "username": username, "success": r.get("success"), "error": r.get("error")})
    ok = sum(1 for x in results if x["success"])
    return jsonify({"success": True, "updated": ok, "total": len(results), "results": results})


@api.route('/accounts/bulk-set-profile-photo', methods=['POST'])
def bulk_set_profile_photo():
    """Set the same profile photo for all accounts with session. Expects multipart form with 'photo' file."""
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
        with get_db_context() as db:
            accounts = db.query(Account).filter(Account.session_string.isnot(None)).order_by(Account.id).all()
        if not accounts:
            return jsonify({"error": "No accounts with session"}), 400
        results = []
        for acc in accounts:
            r = run_async(client_manager.set_account_profile_photo(acc.id, tmp.name))
            results.append({"account_id": acc.id, "success": r.get("success"), "error": r.get("error")})
        ok = sum(1 for x in results if x["success"])
        return jsonify({"success": True, "updated": ok, "total": len(results), "results": results})
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


@api.route('/accounts/<int:account_id>', methods=['DELETE'])
def delete_account(account_id):
    """Permanently remove account from DB and from client manager."""
    from src.clients.manager import client_manager
    from src.core.models import Story, Task
    with get_db_context() as db:
        account = db.query(Account).filter(Account.id == account_id).first()
        if not account:
            return jsonify({"error": "Account not found"}), 404
        phone = account.phone_number
        # Remove related data: delete stories, unlink tasks
        db.query(Story).filter(Story.account_id == account_id).delete(synchronize_session=False)
        db.query(Task).filter(Task.account_id == account_id).update({"account_id": None}, synchronize_session=False)
        db.delete(account)
        db.commit()
    try:
        run_async(client_manager.remove_account(account_id))
    except Exception as e:
        logger.warning("remove_account after delete", account_id=account_id, error=str(e))
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
    return jsonify(result)


@api.route('/accounts/auth/qr-start', methods=['POST'])
def qr_start():
    """Start QR login – no SMS or code needed. Scan with Telegram on your phone."""
    from src.clients.manager import start_qr_login
    result = start_qr_login()
    # normalize token types (some implementations return int)
    try:
        if isinstance(result, dict) and "token" in result and result["token"] is not None:
            result["token"] = str(result["token"])
    except Exception:
        pass

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

        logger.info("import_tdata: discovered %d candidate(s)", len(candidates))

        imported = 0
        updated = 0
        results: list[dict] = []
        for c in candidates:
            idx = c.get("index", len(results) + 1)
            source = c.get("source", "?")
            session_string = c.get("session_string", "")
            ts = datetime.utcnow().isoformat() + "Z"
            try:
                result = run_async(client_manager.import_session_string(session_string))
                if result.get("success"):
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
        logger.info("import_tdata: imported=%d updated=%d failed=%d total=%d", imported, updated, failed, len(candidates))

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
        return jsonify({
            "success": True,
            "message": "; ".join(msg_parts) if msg_parts else "Done",
            "imported": imported,
            "updated": updated,
            "failed": failed,
            "total": len(candidates),
            "total_discovered": len(candidates),
            "results": results,
            "errors": [{"index": r["index"], "error": r.get("error")} for r in results if r.get("status") == "failed"][:50],
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
# Stories API
# ============================================
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


@api.route('/stories/publish', methods=['POST'])
def publish_story():
    """Publish a new story. If mentions not provided, auto-select from discovered users (like the bot)."""
    from src.stories.publisher import story_publisher
    from src.clients.manager import client_manager
    data = request.get_json()
    account_id = data.get('account_id')
    media_path = data.get('media_path')
    caption = data.get('caption') or ''
    mentions = data.get('mentions', [])
    mentions_count = int(data.get('mentions_count', 5))
    if not account_id or not media_path:
        return jsonify({"error": "account_id and media_path required"}), 400
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
                "created_at": _dt_iso(account.created_at),
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
def index():
    """Dashboard home page"""
    return render_template('index.html')


@web.route('/accounts')
def accounts_page():
    """Accounts management page"""
    return render_template('accounts.html')


@web.route('/stories')
def stories_page():
    """Stories page"""
    return render_template('stories.html')


@web.route('/discovery')
def discovery_page():
    """User discovery page"""
    return render_template('discovery.html')


@web.route('/campaigns')
def campaigns_page():
    """Campaigns page"""
    return render_template('campaigns.html')


@web.route('/scheduler')
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
