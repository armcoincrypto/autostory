"""
Scheduler API routes - Targets, Templates, Bindings, Schedule, Logs
"""
import json
from datetime import datetime

from flask import Blueprint, jsonify, request

from src.core.database import get_db_context
from src.core.models import Account, AccountStatus
from src.core.scheduler_models import (
    ChatTarget, AccountTargetBinding, MessageTemplate,
    ScheduleProfile, ScheduleRule, ScheduledJob, MessageDelivery,
    MessageType
)
from src.scheduler.renderer import render_template

scheduler_api = Blueprint('scheduler_api', __name__, url_prefix='/api/v1')


# ============ Targets ============
@scheduler_api.route('/targets', methods=['GET'])
def list_targets():
    with get_db_context() as db:
        targets = db.query(ChatTarget).all()
        return jsonify([{
            "id": t.id,
            "tg_id": t.tg_id,
            "username": t.username,
            "invite_link": t.invite_link,
            "title": t.title,
            "chat_type": t.chat_type,
            "is_verified": t.is_verified,
        } for t in targets])


@scheduler_api.route('/targets', methods=['POST'])
def create_target():
    data = request.get_json() or {}
    with get_db_context() as db:
        t = ChatTarget(
            username=data.get("username"),
            invite_link=data.get("invite_link"),
            tg_id=data.get("tg_id"),
            chat_type=data.get("chat_type", "channel")
        )
        db.add(t)
        db.refresh(t)
        return jsonify({"id": t.id, "success": True})


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
        db.refresh(b)
        return jsonify({"id": b.id, "success": True})


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
        db.refresh(t)
        return jsonify({"id": t.id, "success": True})


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
            db.refresh(p)
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
        db.refresh(r)
        return jsonify({"id": r.id, "success": True})


# ============ Deliveries (Logs) ============
@scheduler_api.route('/deliveries', methods=['GET'])
def list_deliveries():
    account_id = request.args.get("account_id", type=int)
    limit = request.args.get("limit", 100, type=int)
    with get_db_context() as db:
        q = db.query(MessageDelivery).order_by(MessageDelivery.created_at.desc())
        if account_id:
            q = q.filter(MessageDelivery.account_id == account_id)
        deliveries = q.limit(limit).all()
        return jsonify([{
            "id": d.id,
            "account_id": d.account_id,
            "target_id": d.target_id,
            "type": d.type,
            "status": d.status,
            "sent_at": d.sent_at.isoformat() if d.sent_at else None,
            "error_message": d.error_message,
            "created_at": d.created_at.isoformat(),
        } for d in deliveries])
