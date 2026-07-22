"""
Celery Tasks for STORYFLEET
"""
import asyncio
from datetime import datetime
from typing import List, Optional, Dict, Any
from celery import shared_task
import structlog

import sys
sys.path.insert(0, '/home/user/autostory')
from src.core.models import Account, Story, Task, TaskStatus, TaskType, AccountStatus
from src.core.database import get_db_context

logger = structlog.get_logger(__name__)


def run_async(coro):
    """Helper to run async code in sync context"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def publish_story_task(
    self,
    account_id: int,
    media_path: str,
    caption: Optional[str] = None,
    mentions: Optional[List[int]] = None,
    campaign_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Async task to publish a story

    Args:
        account_id: Account to publish from
        media_path: Path to media file
        caption: Story caption
        mentions: List of user IDs to mention
        campaign_id: Associated campaign

    Returns:
        Publication result
    """
    from src.clients.manager import client_manager
    from src.stories.publisher import story_publisher

    logger.info(
        "Executing publish_story_task",
        account_id=account_id,
        task_id=self.request.id
    )

    from src.core.execution_guard import (
        ACTION_STORY_PUBLISH,
        guard_blocked_story_publish,
        require_execution_allowed,
    )

    blocked = require_execution_allowed(ACTION_STORY_PUBLISH, account_id=int(account_id))
    if blocked is not None:
        logger.warning(
            "publish_story_task_blocked_execution_guard",
            account_id=int(account_id),
            reason=blocked.reason_code,
        )
        return guard_blocked_story_publish(blocked)

    # Update task status in database
    with get_db_context() as db:
        task = db.query(Task).filter(
            Task.celery_task_id == self.request.id
        ).first()
        if task:
            task.status = TaskStatus.RUNNING
            task.started_at = datetime.utcnow()

    async def execute():
        await client_manager.initialize()
        client = await client_manager.get_client(account_id)

        if not client:
            return {
                "success": False,
                "error": "Client not available (session missing, invalid, or unauthorized)",
            }

        if not client.is_connected:
            connected = await client.connect()
            if not connected:
                return {"success": False, "error": "Failed to connect client"}

        return await story_publisher.publish_story(
            client_wrapper=client,
            media_path=media_path,
            caption=caption,
            mentions=mentions or [],
            campaign_id=campaign_id,
        )

    try:
        result = run_async(execute())

        # Update task status
        with get_db_context() as db:
            task = db.query(Task).filter(
                Task.celery_task_id == self.request.id
            ).first()
            if task:
                task.status = TaskStatus.COMPLETED if result.get("success") else TaskStatus.FAILED
                task.completed_at = datetime.utcnow()
                task.result = result
                if not result.get("success"):
                    task.error_message = result.get("error")

        return result

    except Exception as e:
        logger.error("Task failed", error=str(e), task_id=self.request.id)

        # Retry on failure
        try:
            self.retry(exc=e)
        except self.MaxRetriesExceededError:
            with get_db_context() as db:
                task = db.query(Task).filter(
                    Task.celery_task_id == self.request.id
                ).first()
                if task:
                    task.status = TaskStatus.FAILED
                    task.error_message = str(e)

            return {"success": False, "error": str(e)}


@shared_task(bind=True, max_retries=2)
def discover_users_task(
    self,
    channel_usernames: List[str],
    limit_per_channel: int = 500,
) -> Dict[str, Any]:
    """
    Async task to discover users from channels

    Args:
        channel_usernames: List of channel usernames
        limit_per_channel: Max users per channel

    Returns:
        Discovery results
    """
    from src.discovery.scanner import user_discovery

    logger.info(
        "Executing discover_users_task",
        channels=channel_usernames,
        task_id=self.request.id
    )

    try:
        result = run_async(user_discovery.discover_from_channels(
            channel_usernames=channel_usernames,
            limit_per_channel=limit_per_channel
        ))
        return result

    except Exception as e:
        logger.error("Discovery task failed", error=str(e))
        return {"success": False, "error": str(e)}


@shared_task(bind=True, max_retries=1)
def batch_publish_task(
    self,
    media_path: str,
    caption: Optional[str] = None,
    mentions_per_story: int = 5,
    max_stories: int = 10,
    campaign_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Async task for batch story publishing

    Args:
        media_path: Path to media
        caption: Story caption
        mentions_per_story: Mentions per story
        max_stories: Maximum stories to publish
        campaign_id: Associated campaign

    Returns:
        Batch results
    """
    from src.stories.publisher import story_publisher
    from src.clients.manager import client_manager

    logger.info(
        "Executing batch_publish_task",
        max_stories=max_stories,
        task_id=self.request.id
    )

    async def execute():
        await client_manager.initialize()
        await client_manager.connect_all()

        return await story_publisher.publish_batch(
            media_path=media_path,
            caption=caption,
            mentions_per_story=mentions_per_story,
            max_stories=max_stories,
            campaign_id=campaign_id,
        )

    try:
        result = run_async(execute())
        return result

    except Exception as e:
        logger.error("Batch publish failed", error=str(e))
        return {"success": False, "error": str(e)}


@shared_task
def reset_daily_counters() -> Dict[str, Any]:
    """Reset daily counters for all accounts (UTC day; optional Celery backup)."""
    from datetime import datetime, timezone

    from src.stories.daily_story_counter import production_story_day

    logger.info("Resetting daily counters")
    today = production_story_day(datetime.now(timezone.utc))

    with get_db_context() as db:
        accounts = db.query(Account).all()
        for account in accounts:
            account.stories_today = 0
            account.stories_today_on = today
            account.actions_today = 0
            if hasattr(account, "story_attempts_today"):
                account.story_attempts_today = 0

    return {"success": True, "accounts_reset": len(accounts), "stories_today_on": str(today)}


@shared_task
def update_story_views() -> Dict[str, Any]:
    """Update view counts for recent stories"""
    from src.stories.publisher import story_publisher
    from src.clients.manager import client_manager

    logger.info("Updating story views")

    async def execute():
        await client_manager.initialize()

        with get_db_context() as db:
            # Get stories from last 24 hours
            from datetime import timedelta
            cutoff = datetime.utcnow() - timedelta(hours=24)

            stories = db.query(Story).filter(
                Story.published_at >= cutoff,
                Story.is_deleted == False
            ).all()

            updated = 0
            for story in stories:
                client = await client_manager.get_client(story.account_id)
                if client and client.is_connected and story.story_id:
                    views = await story_publisher.get_story_views(client, story.story_id)
                    if views is not None:
                        updated += 1

            return {"success": True, "stories_updated": updated}

    try:
        return run_async(execute())
    except Exception as e:
        return {"success": False, "error": str(e)}


@shared_task
def health_check() -> Dict[str, Any]:
    """System health check"""
    from src.clients.manager import client_manager

    logger.debug("Running health check")

    async def execute():
        status = await client_manager.get_status()
        return {
            "success": True,
            "timestamp": datetime.utcnow().isoformat(),
            "clients": status
        }

    try:
        return run_async(execute())
    except Exception as e:
        return {"success": False, "error": str(e)}


def schedule_story_task(
    account_id: int,
    media_path: str,
    caption: Optional[str] = None,
    mentions: Optional[List[int]] = None,
    campaign_id: Optional[int] = None,
    scheduled_at: Optional[datetime] = None,
) -> str:
    """
    Schedule a story publication task

    Returns:
        Celery task ID
    """
    # Create task record
    with get_db_context() as db:
        task = Task(
            account_id=account_id,
            campaign_id=campaign_id,
            task_type=TaskType.PUBLISH_STORY,
            payload={
                "media_path": media_path,
                "caption": caption,
                "mentions": mentions,
            },
            scheduled_at=scheduled_at,
            status=TaskStatus.PENDING,
        )
        db.add(task)
        db.commit()
        task_db_id = task.id

    # Schedule Celery task
    if scheduled_at and scheduled_at > datetime.utcnow():
        celery_task = publish_story_task.apply_async(
            args=[account_id, media_path, caption, mentions, campaign_id],
            eta=scheduled_at,
        )
    else:
        celery_task = publish_story_task.delay(
            account_id, media_path, caption, mentions, campaign_id
        )

    # Update task with Celery ID
    with get_db_context() as db:
        task = db.query(Task).filter(Task.id == task_db_id).first()
        if task:
            task.celery_task_id = celery_task.id

    logger.info(
        "Story task scheduled",
        task_id=celery_task.id,
        account_id=account_id,
        scheduled_at=scheduled_at
    )

    return celery_task.id
