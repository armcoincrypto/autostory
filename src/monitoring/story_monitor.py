"""
Story Monitor - Track story performance and send alerts
"""
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
import structlog
from sqlalchemy import func

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.stories import GetStoriesByIDRequest, GetAllStoriesRequest

from config.settings import settings
from src.core.database import get_db_context
from src.core.models import Account, Story, DiscoveredUser, AccountStatus

logger = structlog.get_logger(__name__)


class StoryMonitor:
    """Monitor story performance and track analytics"""

    def __init__(self):
        self._clients: Dict[int, TelegramClient] = {}
        self._running = False

    async def get_client(self, account_id: int) -> Optional[TelegramClient]:
        """Get Telegram client for account"""
        if account_id in self._clients and self._clients[account_id].is_connected():
            return self._clients[account_id]

        with get_db_context() as db:
            account = db.query(Account).filter(Account.id == account_id).first()
            if not account or not account.session_string:
                return None
            session_string = account.session_string

        client = TelegramClient(
            StringSession(session_string),
            settings.telegram.api_id,
            settings.telegram.api_hash
        )
        await client.connect()
        self._clients[account_id] = client
        return client

    async def update_story_views(self, story_id: int, account_id: int) -> Optional[int]:
        """Get current view count for a story"""
        try:
            client = await self.get_client(account_id)
            if not client:
                return None

            # Get story by ID
            result = await client(GetStoriesByIDRequest(
                peer="me",
                id=[story_id]
            ))

            if result.stories:
                story = result.stories[0]
                views = getattr(story, 'views', None)
                if views:
                    view_count = getattr(views, 'views_count', 0)

                    # Update database
                    with get_db_context() as db:
                        db_story = db.query(Story).filter(Story.story_id == story_id).first()
                        if db_story:
                            db_story.views_count = view_count
                            db.commit()

                    return view_count
            return 0

        except Exception as e:
            logger.warning("Failed to get story views", story_id=story_id, error=str(e))
            return None

    def get_system_stats(self) -> Dict[str, Any]:
        """Get comprehensive system statistics"""
        with get_db_context() as db:
            now = datetime.utcnow()
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

            # Account stats
            total_accounts = db.query(Account).count()
            active_accounts = db.query(Account).filter(
                Account.status == AccountStatus.ACTIVE
            ).count()
            banned_accounts = db.query(Account).filter(
                Account.status == AccountStatus.BANNED
            ).count()
            flood_wait_accounts = db.query(Account).filter(
                Account.status == AccountStatus.FLOOD_WAIT
            ).count()

            # Story stats
            total_stories = db.query(Story).count()
            stories_today = db.query(Story).filter(
                Story.published_at >= today_start
            ).count()

            # User stats
            total_users = db.query(DiscoveredUser).count()
            mentioned_users = db.query(DiscoveredUser).filter(
                DiscoveredUser.times_mentioned > 0
            ).count()
            available_users = db.query(DiscoveredUser).filter(
                DiscoveredUser.times_mentioned == 0,
                DiscoveredUser.username.isnot(None)
            ).count()

            # View stats
            total_views = db.query(func.sum(Story.views_count)).scalar() or 0

            # Recent stories with views
            recent_stories = db.query(Story).order_by(
                Story.published_at.desc()
            ).limit(5).all()

            recent_story_stats = []
            for s in recent_stories:
                recent_story_stats.append({
                    "story_id": s.story_id,
                    "views": s.views_count or 0,
                    "published": s.published_at.isoformat() if s.published_at else None,
                })

        return {
            "timestamp": now.isoformat(),
            "accounts": {
                "total": total_accounts,
                "active": active_accounts,
                "banned": banned_accounts,
                "flood_wait": flood_wait_accounts,
            },
            "stories": {
                "total": total_stories,
                "today": stories_today,
                "total_views": total_views,
            },
            "users": {
                "total": total_users,
                "mentioned": mentioned_users,
                "available": available_users,
            },
            "recent_stories": recent_story_stats,
        }

    def format_stats_message(self, stats: Dict[str, Any]) -> str:
        """Format stats as readable message"""
        return f"""📊 **STORYFLEET Analytics**

**Accounts**
├ Total: {stats['accounts']['total']}
├ Active: {stats['accounts']['active']} ✅
├ Banned: {stats['accounts']['banned']} 🚫
└ Flood Wait: {stats['accounts']['flood_wait']} ⏳

**Stories**
├ Total Published: {stats['stories']['total']}
├ Today: {stats['stories']['today']}
└ Total Views: {stats['stories']['total_views']} 👁

**Users**
├ Total Discovered: {stats['users']['total']}
├ Already Mentioned: {stats['users']['mentioned']}
└ Available: {stats['users']['available']} 📤

**Recent Stories**
""" + "\n".join([
            f"• Story #{s['story_id']}: {s['views']} views"
            for s in stats['recent_stories']
        ])

    async def run_view_updates(self, interval_minutes: int = 30):
        """Background task to update story views"""
        self._running = True
        logger.info("Starting story view monitor", interval=interval_minutes)

        while self._running:
            try:
                # Get stories from last 24 hours
                with get_db_context() as db:
                    cutoff = datetime.utcnow() - timedelta(hours=24)
                    recent_stories = db.query(Story).filter(
                        Story.published_at >= cutoff,
                        Story.story_id.isnot(None)
                    ).all()

                    for story in recent_stories:
                        if story.story_id and story.account_id:
                            await self.update_story_views(story.story_id, story.account_id)
                            await asyncio.sleep(2)  # Rate limit

                logger.info("Updated story views", count=len(recent_stories))

            except Exception as e:
                logger.error("Error updating story views", error=str(e))

            await asyncio.sleep(interval_minutes * 60)

    def stop(self):
        """Stop the monitor"""
        self._running = False


# Singleton instance
story_monitor = StoryMonitor()
