"""
Scheduler worker - main loop: generate jobs, execute due jobs
"""
import asyncio
from datetime import datetime, date

from src.core.database import init_db
from src.core.scheduler_models import ScheduledJob, JobStatus
from src.clients.manager import client_manager
from .generator import generate_jobs_for_date
from .executor import execute_job
import structlog

logger = structlog.get_logger(__name__)

LOOP_INTERVAL_SEC = 45
LAST_GEN_DATE: date = None


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
