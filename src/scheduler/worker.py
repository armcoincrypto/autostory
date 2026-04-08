"""
Scheduler worker - main loop: generate jobs, execute due jobs
"""
import asyncio
import os
import urllib.request
import urllib.error
from datetime import datetime, date, timedelta

from src.core.database import init_db
from src.core.scheduler_models import ScheduledJob, JobStatus
from src.clients.manager import client_manager
from .generator import generate_jobs_for_date
from .executor import execute_job
import structlog

logger = structlog.get_logger(__name__)

LOOP_INTERVAL_SEC = 45
LAST_GEN_DATE: date = None

# Trigger a fleet health check via the web API every this many hours
HEALTH_CHECK_INTERVAL_HOURS = 6
_last_health_check_trigger: datetime = None


def _trigger_fleet_health_check() -> None:
    """
    Ask the web process to run a fleet health check by POSTing to its internal
    API. Runs in the scheduler process (separate from Gunicorn). Fails silently
    so a web hiccup never crashes the scheduler loop.
    """
    global _last_health_check_trigger
    token = os.environ.get("DASHBOARD_ADMIN_TOKEN", "")
    port = int(os.environ.get("PORT", 8000))
    url = f"http://127.0.0.1:{port}/api/accounts/healthcheck/start"
    try:
        req = urllib.request.Request(
            url, data=b"", method="POST",
            headers={"X-Admin-Token": token, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            _last_health_check_trigger = datetime.utcnow()
            logger.info("Fleet health check triggered by scheduler", status=r.status)
    except Exception as e:
        logger.warning("Could not trigger fleet health check", error=str(e))


async def run_scheduler_loop():
    """Main scheduler loop"""
    init_db()
    await client_manager.initialize()
    await client_manager.connect_all()

    global LAST_GEN_DATE
    logger.info("Scheduler worker started")

    while True:
        try:
            now = datetime.utcnow()
            today = now.date()

            if LAST_GEN_DATE != today:
                created = generate_jobs_for_date(today)
                LAST_GEN_DATE = today
                if created:
                    logger.info("Generated jobs for date", date=str(today), count=created)

            # Periodic fleet health check — keep health_checked_at fresh
            if _last_health_check_trigger is None or \
                    now - _last_health_check_trigger > timedelta(hours=HEALTH_CHECK_INTERVAL_HOURS):
                _trigger_fleet_health_check()

            from src.core.database import get_db_context
            with get_db_context() as db:
                due = db.query(ScheduledJob).filter(
                    ScheduledJob.status == JobStatus.PENDING,
                    ScheduledJob.run_at <= now
                ).order_by(ScheduledJob.run_at).limit(10).all()
                job_ids = [j.id for j in due]

            for jid in job_ids:
                try:
                    await execute_job(jid)
                except Exception as e:
                    logger.error("Job execution failed", job_id=jid, error=str(e))
                await asyncio.sleep(2)

        except Exception as e:
            logger.error("Scheduler loop error", error=str(e))

        await asyncio.sleep(LOOP_INTERVAL_SEC)


def main():
    asyncio.run(run_scheduler_loop())


if __name__ == "__main__":
    main()
