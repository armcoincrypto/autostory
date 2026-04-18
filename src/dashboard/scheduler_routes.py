"""
Scheduler API routes - Targets, Templates, Bindings, Schedule, Logs
"""
import asyncio
import json
from datetime import datetime, date

from flask import Blueprint, jsonify, request

from src.core.database import get_db_context
from src.core.models import Account, AccountStatus
from src.core.scheduler_models import (
    ChatTarget, AccountTargetBinding, MessageTemplate,
    ScheduleProfile, ScheduleRule, ScheduledJob, MessageDelivery,
    MessageType, JobStatus
)
from src.scheduler.renderer import render_template
from src.scheduler.executor import execute_job

scheduler_api = Blueprint('scheduler_api', __name__, url_prefix='/api/v1')


# ============ Stats ============
@scheduler_api.route('/stats', methods=['GET'])
def scheduler_stats():
    today_start = datetime.combine(date.today(), datetime.min.time())
    with get_db_context() as db:
        sent_today = db.query(MessageDelivery).filter(
            MessageDelivery.status == 'SENT',
            MessageDelivery.created_at >= today_start,
        ).count()
        failed_today = db.query(MessageDelivery).filter(
            MessageDelivery.status == 'FAILED',
            MessageDelivery.created_at >= today_start,
        ).count()
        active_accounts = db.query(ScheduleProfile).filter(
            ScheduleProfile.is_enabled == True
        ).count()
        target_groups = db.query(ChatTarget).count()
        pending_jobs = db.query(ScheduledJob).filter(
            ScheduledJob.status == JobStatus.PENDING
        ).count()
    return jsonify({
        'sent_today': sent_today,
        'failed_today': failed_today,
        'active_accounts': active_accounts,
        'target_groups': target_groups,
        'pending_jobs': pending_jobs,
    })


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
        db.flush()  # Persist and assign ID (refresh fails on pending instances)
        return jsonify({"id": t.id, "success": True})


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
@scheduler_api.route('/schedule/profiles', methods=['GET'])
def list_schedule_profiles():
    with get_db_context() as db:
        profiles = db.query(ScheduleProfile).all()
        return jsonify([{
            "id": p.id,
            "account_id": p.account_id,
            "is_enabled": p.is_enabled,
            "timezone": p.timezone,
            "min_interval_sec": p.min_interval_sec,
            "daily_cap_total": p.daily_cap_total,
            "jitter_sec": p.jitter_sec,
        } for p in profiles])


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
@scheduler_api.route('/schedule/rules', methods=['GET'])
def list_all_schedule_rules():
    with get_db_context() as db:
        rules = db.query(ScheduleRule).all()
        return jsonify([{
            "id": r.id,
            "account_id": r.account_id,
            "type": r.type,
            "times_json": r.times_json,
            "target_mode": r.target_mode,
            "is_enabled": r.is_enabled,
        } for r in rules])


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


# ============ Bulk Apply ============
@scheduler_api.route('/schedule/bulk-apply', methods=['POST'])
def bulk_apply_schedule():
    """Copy profile + rules + template (+ bindings) from one account to many."""
    data = request.get_json() or {}
    source_id = data.get('source_account_id')
    target_ids = data.get('target_account_ids', [])
    copy_profile = data.get('copy_profile', True)
    copy_rules = data.get('copy_rules', True)
    copy_template = data.get('copy_template', True)
    copy_bindings = data.get('copy_bindings', True)

    if not source_id:
        return jsonify({'error': 'source_account_id required'}), 400
    if not target_ids or not isinstance(target_ids, list):
        return jsonify({'error': 'target_account_ids must be a non-empty list'}), 400
    if len(target_ids) > 50:
        return jsonify({'error': 'Maximum 50 target accounts per request'}), 400
    target_ids = [tid for tid in target_ids if tid != source_id]

    # Load source data into plain dicts (session-independent)
    profile_data = rules_data = templates_data = bindings_data = None
    with get_db_context() as db:
        if copy_profile:
            src = db.query(ScheduleProfile).filter(ScheduleProfile.account_id == source_id).first()
            if not src:
                return jsonify({'error': 'Source account has no schedule profile. Save settings first.'}), 400
            profile_data = {
                'is_enabled': src.is_enabled, 'timezone': src.timezone,
                'min_interval_sec': src.min_interval_sec, 'daily_cap_total': src.daily_cap_total,
                'daily_cap_promo': src.daily_cap_promo, 'daily_cap_info': src.daily_cap_info,
                'jitter_sec': src.jitter_sec, 'quiet_hours_json': src.quiet_hours_json,
            }
        if copy_rules:
            rules_data = [
                {'type': r.type, 'times_json': r.times_json, 'target_mode': r.target_mode,
                 'selected_target_ids_json': r.selected_target_ids_json, 'is_enabled': r.is_enabled}
                for r in db.query(ScheduleRule).filter(ScheduleRule.account_id == source_id).all()
            ]
        if copy_template:
            templates_data = [
                {'type': t.type, 'name': t.name, 'body': t.body,
                 'is_active': t.is_active, 'weight': t.weight}
                for t in db.query(MessageTemplate).filter(
                    MessageTemplate.account_id == source_id,
                    MessageTemplate.scope == 'ACCOUNT'
                ).all()
            ]
        if copy_bindings:
            bindings_data = [
                {'target_id': b.target_id, 'can_post': b.can_post,
                 'allowed_types': b.allowed_types, 'daily_cap': b.daily_cap}
                for b in db.query(AccountTargetBinding).filter(
                    AccountTargetBinding.account_id == source_id
                ).all()
            ]

    results = []
    for target_id in target_ids:
        try:
            with get_db_context() as db:
                if profile_data:
                    existing = db.query(ScheduleProfile).filter(
                        ScheduleProfile.account_id == target_id
                    ).first()
                    if existing:
                        for k, v in profile_data.items():
                            setattr(existing, k, v)
                    else:
                        db.add(ScheduleProfile(account_id=target_id, **profile_data))

                if rules_data is not None:
                    for r in db.query(ScheduleRule).filter(
                        ScheduleRule.account_id == target_id
                    ).all():
                        db.delete(r)
                    db.flush()
                    for rd in rules_data:
                        db.add(ScheduleRule(account_id=target_id, **rd))

                if templates_data is not None:
                    for t in db.query(MessageTemplate).filter(
                        MessageTemplate.account_id == target_id,
                        MessageTemplate.scope == 'ACCOUNT'
                    ).all():
                        db.delete(t)
                    db.flush()
                    for td in templates_data:
                        db.add(MessageTemplate(scope='ACCOUNT', account_id=target_id, **td))

                if bindings_data is not None:
                    existing_tids = {b.target_id for b in db.query(AccountTargetBinding).filter(
                        AccountTargetBinding.account_id == target_id
                    ).all()}
                    for bd in bindings_data:
                        if bd['target_id'] not in existing_tids:
                            db.add(AccountTargetBinding(account_id=target_id, **bd))

            results.append({'account_id': target_id, 'success': True})
        except Exception as e:
            results.append({'account_id': target_id, 'success': False, 'error': str(e)})

    succeeded = sum(1 for r in results if r['success'])
    return jsonify({
        'results': results, 'total': len(results),
        'succeeded': succeeded, 'failed': len(results) - succeeded,
    })


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
            return jsonify({"error": "No binding for this account-target pair"}), 400
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
    try:
        asyncio.run(execute_job(job_id))
    except Exception as e:
        return jsonify({"error": str(e), "job_id": job_id}), 500
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
