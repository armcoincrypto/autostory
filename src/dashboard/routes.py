"""
Dashboard Routes - API and Web endpoints
"""
import asyncio
from functools import wraps
from flask import Blueprint, jsonify, request, render_template
from flask_login import login_required, current_user
import structlog

import sys
sys.path.insert(0, '/home/user/autostory')
from src.core.models import Account, Story, DiscoveredUser, Campaign, Task, AccountStatus
from src.core.database import get_db_context

logger = structlog.get_logger(__name__)


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
    """List all accounts"""
    with get_db_context() as db:
        accounts = db.query(Account).all()
        return jsonify([
            {
                "id": a.id,
                "phone_number": a.phone_number,
                "username": a.username,
                "first_name": a.first_name,
                "status": a.status.value,
                "last_active": a.last_active.isoformat() if a.last_active else None,
                "stories_today": a.stories_today,
            }
            for a in accounts
        ])


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
            "last_active": account.last_active.isoformat() if account.last_active else None,
            "last_error": account.last_error,
            "stories_today": account.stories_today,
            "actions_today": account.actions_today,
            "created_at": account.created_at.isoformat(),
        })


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
        users = query.order_by(
            DiscoveredUser.discovered_at.desc()
        ).offset((page - 1) * per_page).limit(per_page).all()

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

        return jsonify({
            "success": True,
            "campaign_id": campaign.id,
            "name": campaign.name
        })


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
