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
                    "last_active": r.last_active.isoformat() if r.last_active else None,
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
                "status": a.status.value,
                "purpose": p,
                "last_active": a.last_active.isoformat() if a.last_active else None,
                "stories_today": a.stories_today,
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
            "status": account.status.value,
            "purpose": getattr(account, "purpose", None) or "both",
            "last_active": account.last_active.isoformat() if account.last_active else None,
            "last_error": account.last_error,
            "stories_today": account.stories_today,
            "actions_today": account.actions_today,
            "created_at": account.created_at.isoformat(),
        })


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
# Discovery API
# ============================================
@api.route('/discovery/users', methods=['GET'])
def list_discovered_users():
    """List discovered users"""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    mentioned = request.args.get('mentioned', None)
    with get_db_context() as db:
        query = db.query(DiscoveredUser)
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
                    "times_mentioned": u.times_mentioned,
                    "discovered_at": u.discovered_at.isoformat(),
                }
                for u in users
            ]
        })


@api.route('/discovery/scan', methods=['POST'])
def scan_channel():
    """Scan a channel for users"""
    from src.discovery.scanner import user_discovery
    data = request.get_json()
    channels = data.get('channels', [])
    if not channels:
        return jsonify({"error": "channels list required"}), 400
    result = run_async(user_discovery.discover_from_channels(
        channel_usernames=channels,
        limit_per_channel=data.get('limit', 500)
    ))
    return jsonify(result)


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
