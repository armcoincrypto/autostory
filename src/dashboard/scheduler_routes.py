"""
Scheduler API routes - Targets, Templates, Bindings, Schedule, Logs
"""
import json
import logging
import os
import subprocess
import sys
import urllib.request
import urllib.error
from datetime import datetime

from flask import Blueprint, jsonify, request

from config.settings import settings
from src.core.database import get_db_context
from src.core.models import Account, AccountStatus
from src.core.scheduler_models import (
    ChatTarget, AccountTargetBinding, MessageTemplate,
    ScheduleProfile, ScheduleRule, ScheduledJob, MessageDelivery,
    MessageType, JobStatus
)
from src.scheduler.renderer import render_template

scheduler_api = Blueprint('scheduler_api', __name__, url_prefix='/api/v1')


def _proxy_to_server():
    """When proxy URL is set, forward all scheduler API requests to the server."""
    proxy_url = getattr(settings.dashboard, "run_now_proxy_url", None) or os.environ.get("DASHBOARD_RUN_NOW_PROXY_URL")
    if not proxy_url:
        return None
    proxy_url = proxy_url.rstrip("/")
    url = proxy_url + request.full_path  # e.g. http://server:5000/api/v1/bindings?account_id=2
    try:
        body = request.get_data() or None
        headers = {"Content-Type": "application/json"} if body else {}
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


@scheduler_api.before_request
def maybe_proxy():
    """Proxy all scheduler API to server when DASHBOARD_RUN_NOW_PROXY_URL is set."""
    rv = _proxy_to_server()
    if rv is not None:
        return rv


# ============ Targets ============
@scheduler_api.route('/targets', methods=['GET'])
def list_targets():
    try:
        with get_db_context() as db:
            targets = db.query(ChatTarget).all()
            return jsonify([{
                "id": t.id,
                "tg_id": getattr(t, "tg_id", None),
                "username": t.username,
                "invite_link": t.invite_link,
                "title": t.title,
                "chat_type": t.chat_type,
                "is_verified": getattr(t, "is_verified", False),
            } for t in targets])
    except Exception as e:
        logging.getLogger(__name__).exception("list_targets failed: %s", e)
        return jsonify([])


@scheduler_api.route('/targets', methods=['POST'])
def create_target():
    data = request.get_json() or {}
    with get_db_context() as db:
        t = ChatTarget(
            username=data.get("username"),
            invite_link=data.get("invite_link"),
            tg_id=data.get("tg_id"),
            title=data.get("title"),
            chat_type=data.get("chat_type", "channel")
        )
        db.add(t)
        db.flush()  # Persist and assign ID (refresh fails on pending instances)
        return jsonify({"id": t.id, "success": True})


@scheduler_api.route('/targets/<int:target_id>', methods=['PATCH'])
def update_target(target_id):
    """Update target (e.g. set title for dialog matching when account joined manually)."""
    data = request.get_json() or {}
    with get_db_context() as db:
        t = db.query(ChatTarget).filter(ChatTarget.id == target_id).first()
        if not t:
            return jsonify({"error": "Target not found"}), 404
        if "title" in data:
            t.title = data["title"] or None
        if "username" in data:
            t.username = data["username"] or None
        if "invite_link" in data:
            t.invite_link = data["invite_link"] or None
        return jsonify({"success": True})


@scheduler_api.route('/targets/<int:target_id>', methods=['DELETE'])
def delete_target(target_id):
    with get_db_context() as db:
        t = db.query(ChatTarget).filter(ChatTarget.id == target_id).first()
        if not t:
            return jsonify({"error": "Target not found"}), 404
        db.delete(t)
        return jsonify({"success": True})


@scheduler_api.route('/targets/<int:target_id>/verify', methods=['POST'])
def verify_target(target_id):
    """Mark target as verified (manual; real verification would need Telethon lookup)."""
    with get_db_context() as db:
        t = db.query(ChatTarget).filter(ChatTarget.id == target_id).first()
        if not t:
            return jsonify({"error": "Target not found"}), 404
        t.is_verified = True
        t.verified_at = datetime.utcnow()
        return jsonify({"success": True, "is_verified": True})


# ============ Bindings ============
@scheduler_api.route('/bindings', methods=['GET'])
def list_bindings():
    account_id = request.args.get("account_id", type=int)
    with get_db_context() as db:
        q = db.query(AccountTargetBinding)
        if account_id:
            q = q.filter(AccountTargetBinding.account_id == account_id)
        bindings = q.all()
        return jsonify([{
            "id": b.id,
            "account_id": b.account_id,
            "target_id": b.target_id,
            "can_post": b.can_post,
            "allowed_types": b.allowed_types,
            "daily_cap": b.daily_cap,
        } for b in bindings])


@scheduler_api.route('/bindings', methods=['POST'])
def create_binding():
    data = request.get_json() or {}
    account_id = data.get("account_id")
    target_id = data.get("target_id")
    if not account_id or not target_id:
        return jsonify({"error": "account_id and target_id required"}), 400
    with get_db_context() as db:
        exists = db.query(AccountTargetBinding).filter(
            AccountTargetBinding.account_id == account_id,
            AccountTargetBinding.target_id == target_id
        ).first()
        if exists:
            return jsonify({"error": "Binding already exists", "id": exists.id}), 400
        b = AccountTargetBinding(
            account_id=account_id,
            target_id=target_id,
            can_post=data.get("can_post", True),
            allowed_types=data.get("allowed_types", "PROMO,INFO"),
            daily_cap=data.get("daily_cap")
        )
        db.add(b)
        db.flush()
        return jsonify({"id": b.id, "success": True})


@scheduler_api.route('/bindings/<int:binding_id>', methods=['PUT'])
def update_binding(binding_id):
    data = request.get_json() or {}
    with get_db_context() as db:
        b = db.query(AccountTargetBinding).filter(AccountTargetBinding.id == binding_id).first()
        if not b:
            return jsonify({"error": "Binding not found"}), 404
        for k in ["can_post", "allowed_types", "daily_cap"]:
            if k in data:
                setattr(b, k, data[k])
        return jsonify({"success": True})


@scheduler_api.route('/bindings/<int:binding_id>', methods=['DELETE'])
def delete_binding(binding_id):
    with get_db_context() as db:
        b = db.query(AccountTargetBinding).filter(AccountTargetBinding.id == binding_id).first()
        if not b:
            return jsonify({"error": "Binding not found"}), 404
        db.delete(b)
        return jsonify({"success": True})


# ============ Templates ============
@scheduler_api.route('/templates', methods=['GET'])
def list_templates():
    msg_type = request.args.get("type")
    scope = request.args.get("scope")
    with get_db_context() as db:
        q = db.query(MessageTemplate)
        if msg_type:
            q = q.filter(MessageTemplate.type == msg_type)
        if scope:
            q = q.filter(MessageTemplate.scope == scope)
        templates = q.all()
        return jsonify([{
            "id": t.id,
            "type": t.type,
            "scope": t.scope,
            "account_id": t.account_id,
            "target_id": t.target_id,
            "binding_id": t.binding_id,
            "name": t.name,
            "body": t.body,
            "is_active": t.is_active,
            "weight": t.weight,
        } for t in templates])


@scheduler_api.route('/templates', methods=['POST'])
def create_template():
    data = request.get_json() or {}
    if not data.get("type") or not data.get("body"):
        return jsonify({"error": "type and body required"}), 400
    with get_db_context() as db:
        t = MessageTemplate(
            type=data["type"],
            scope=data.get("scope", "GLOBAL"),
            account_id=data.get("account_id"),
            target_id=data.get("target_id"),
            binding_id=data.get("binding_id"),
            name=data.get("name", "Template"),
            body=data["body"],
            is_active=data.get("is_active", True),
            weight=data.get("weight", 100)
        )
        db.add(t)
        db.flush()
        return jsonify({"id": t.id, "success": True})


@scheduler_api.route('/templates/<int:template_id>', methods=['PUT'])
def update_template(template_id):
    data = request.get_json() or {}
    with get_db_context() as db:
        t = db.query(MessageTemplate).filter(MessageTemplate.id == template_id).first()
        if not t:
            return jsonify({"error": "Template not found"}), 404
        for k in ["type", "scope", "name", "body", "is_active", "weight", "account_id", "target_id", "binding_id"]:
            if k in data:
                setattr(t, k, data[k])
        return jsonify({"success": True})


@scheduler_api.route('/templates/<int:template_id>', methods=['DELETE'])
def delete_template(template_id):
    with get_db_context() as db:
        t = db.query(MessageTemplate).filter(MessageTemplate.id == template_id).first()
        if not t:
            return jsonify({"error": "Template not found"}), 404
        db.delete(t)
        return jsonify({"success": True})


@scheduler_api.route('/templates/preview', methods=['POST'])
def preview_template():
    data = request.get_json() or {}
    body = data.get("body")
    if not body:
        return jsonify({"error": "body required"}), 400
    rendered = render_template(
        body,
        account_name=data.get("account_name", "Account"),
        chat_title=data.get("chat_title", "Chat"),
    )
    return jsonify({"rendered": rendered})


# ============ Schedule Profile ============
@scheduler_api.route('/schedule/profile/<int:account_id>', methods=['GET'])
def get_schedule_profile(account_id):
    with get_db_context() as db:
        p = db.query(ScheduleProfile).filter(ScheduleProfile.account_id == account_id).first()
        if not p:
            return jsonify({"account_id": account_id, "is_enabled": False})
        return jsonify({
            "id": p.id,
            "account_id": p.account_id,
            "is_enabled": p.is_enabled,
            "timezone": p.timezone,
            "min_interval_sec": p.min_interval_sec,
            "daily_cap_total": p.daily_cap_total,
            "daily_cap_promo": p.daily_cap_promo,
            "daily_cap_info": p.daily_cap_info,
            "jitter_sec": p.jitter_sec,
            "quiet_hours_json": p.quiet_hours_json,
        })


@scheduler_api.route('/schedule/profile/<int:account_id>', methods=['PUT'])
def update_schedule_profile(account_id):
    data = request.get_json() or {}
    with get_db_context() as db:
        p = db.query(ScheduleProfile).filter(ScheduleProfile.account_id == account_id).first()
        if not p:
            p = ScheduleProfile(account_id=account_id)
            db.add(p)
            db.flush()
        for k in ["is_enabled", "timezone", "min_interval_sec", "daily_cap_total",
                  "daily_cap_promo", "daily_cap_info", "jitter_sec", "quiet_hours_json"]:
            if k in data:
                setattr(p, k, data[k])
        return jsonify({"success": True})


# ============ Schedule Rules ============
@scheduler_api.route('/schedule/rules/<int:account_id>', methods=['GET'])
def list_schedule_rules(account_id):
    with get_db_context() as db:
        rules = db.query(ScheduleRule).filter(ScheduleRule.account_id == account_id).all()
        return jsonify([{
            "id": r.id,
            "account_id": r.account_id,
            "type": r.type,
            "times_json": r.times_json,
            "target_mode": r.target_mode,
            "selected_target_ids_json": r.selected_target_ids_json,
            "is_enabled": r.is_enabled,
        } for r in rules])


@scheduler_api.route('/schedule/rules/<int:account_id>', methods=['POST'])
def create_schedule_rule(account_id):
    data = request.get_json() or {}
    if not data.get("type"):
        return jsonify({"error": "type (PROMO or INFO) required"}), 400
    times = data.get("times_json", ["10:00", "18:00"])
    if isinstance(times, list):
        times = json.dumps(times)
    with get_db_context() as db:
        r = ScheduleRule(
            account_id=account_id,
            type=data["type"],
            times_json=times,
            target_mode=data.get("target_mode", "ALL_BOUND"),
            selected_target_ids_json=json.dumps(data.get("selected_target_ids", [])) if data.get("selected_target_ids") else None,
            is_enabled=data.get("is_enabled", True)
        )
        db.add(r)
        db.flush()
        return jsonify({"id": r.id, "success": True})


@scheduler_api.route('/schedule/rules/<int:account_id>/<int:rule_id>', methods=['DELETE'])
def delete_schedule_rule(account_id, rule_id):
    with get_db_context() as db:
        r = db.query(ScheduleRule).filter(
            ScheduleRule.id == rule_id,
            ScheduleRule.account_id == account_id
        ).first()
        if not r:
            return jsonify({"error": "Rule not found"}), 404
        db.delete(r)
        return jsonify({"success": True})


# ============ Run Now (Test) ============
@scheduler_api.route('/jobs/run-now', methods=['POST'])
def run_job_now():
    """Create and execute a job immediately for testing."""
    data = request.get_json() or {}
    account_id = data.get("account_id")
    target_id = data.get("target_id")
    msg_type = data.get("type")
    if not all([account_id, target_id, msg_type]):
        return jsonify({"error": "account_id, target_id, type required"}), 400
    if msg_type not in ("PROMO", "INFO"):
        return jsonify({"error": "type must be PROMO or INFO"}), 400

    with get_db_context() as db:
        binding = db.query(AccountTargetBinding).filter(
            AccountTargetBinding.account_id == account_id,
            AccountTargetBinding.target_id == target_id
        ).first()
        if not binding:
            return jsonify({"error": "No binding for this account-target pair. Click Save settings first."}), 400
        job = ScheduledJob(
            account_id=int(account_id),
            target_id=int(target_id),
            type=msg_type,
            run_at=datetime.utcnow(),
            status=JobStatus.PENDING
        )
        db.add(job)
        db.flush()
        job_id = job.id
    # Run executor in subprocess to avoid Telethon "event loop must not change" error.
    # capture_output=False so logs (account, resolved chat) appear in terminal for debugging
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.run_job_now", str(job_id)],
        cwd=project_root,
        capture_output=False,  # show logs in terminal
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        with get_db_context() as db2:
            d = db2.query(MessageDelivery).filter(
                MessageDelivery.job_id == job_id
            ).order_by(MessageDelivery.created_at.desc()).first()
            err_code = (d.error_code or "SendFailed") if d else "SendFailed"
            err_msg = (d.error_message or proc.stderr or "Send failed") if d else (proc.stderr or "Send failed")
        return jsonify({
            "error": err_msg.strip()[:500] if err_msg else "Send failed",
            "error_code": err_code,
            "job_id": job_id
        }), 500
    return jsonify({"success": True, "job_id": job_id})


# ============ Deliveries (Logs) ============
@scheduler_api.route('/deliveries', methods=['GET'])
def list_deliveries():
    account_id = request.args.get("account_id", type=int)
    target_id = request.args.get("target_id", type=int)
    msg_type = request.args.get("type")
    limit = request.args.get("limit", 100, type=int)
    with get_db_context() as db:
        q = db.query(MessageDelivery).order_by(MessageDelivery.created_at.desc())
        if account_id:
            q = q.filter(MessageDelivery.account_id == account_id)
        if target_id:
            q = q.filter(MessageDelivery.target_id == target_id)
        if msg_type:
            q = q.filter(MessageDelivery.type == msg_type)
        deliveries = q.limit(limit).all()
        return jsonify([{
            "id": d.id,
            "account_id": d.account_id,
            "target_id": d.target_id,
            "type": d.type,
            "status": d.status,
            "sent_at": d.sent_at.isoformat() if d.sent_at else None,
            "error_code": getattr(d, "error_code", None),
            "error_message": d.error_message,
            "created_at": d.created_at.isoformat(),
        } for d in deliveries])
