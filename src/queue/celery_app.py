"""
Celery Application Configuration
"""
from celery import Celery
import structlog

import sys
sys.path.insert(0, '/home/user/autostory')
from config.settings import settings

logger = structlog.get_logger(__name__)

# Create Celery app
celery_app = Celery(
    "storyfleet",
    broker=settings.redis.url,
    backend=settings.redis.url,
    include=["src.queue.tasks"]
)

# Celery configuration
celery_app.conf.update(
    # Task settings
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,

    # Task execution settings
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_time_limit=600,  # 10 minutes max per task
    task_soft_time_limit=540,  # Soft limit at 9 minutes

    # Worker settings
    worker_prefetch_multiplier=1,  # One task at a time for rate limiting
    worker_concurrency=4,  # 4 concurrent workers

    # Rate limiting at task level
    task_default_rate_limit="10/m",  # 10 tasks per minute default

    # Result backend settings
    result_expires=3600,  # Results expire in 1 hour

    # Beat scheduler (for periodic tasks)
    beat_schedule={
        "reset-daily-counters": {
            "task": "src.queue.tasks.reset_daily_counters",
            "schedule": 86400.0,  # Every 24 hours
        },
        "update-story-views": {
            "task": "src.queue.tasks.update_story_views",
            "schedule": 3600.0,  # Every hour
        },
        "health-check": {
            "task": "src.queue.tasks.health_check",
            "schedule": 300.0,  # Every 5 minutes
        },
    },
)

logger.info("Celery app configured", broker=settings.redis.url)
