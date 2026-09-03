"""
Scheduler worker - main loop: generate jobs, execute due jobs
"""
import asyncio
import os
from datetime import datetime, date, timezone

from src.core.database import init_db, get_db_context, run_with_sqlite_lock_retry
from src.ai_agent.account_allowlist import RESERVED_AI_AGENT_ACCOUNT_IDS
from .generator import generate_jobs_for_date
from .executor import execute_job
from .job_claim import claim_due_job
from src.stories.scheduler_integration import story_execution_enabled
import structlog

logger = structlog.get_logger(__name__)

LOOP_INTERVAL_SEC = 45
LAST_GEN_DATE: date = None

LEASE_SECONDS = int(os.environ.get("SCHEDULER_JOB_LEASE_SEC", "900"))
WORKER_ID = os.environ.get("AUTOSTORY_SCHEDULER_WORKER_ID", f"w{os.getpid()}")


async def run_scheduler_loop():
    """Main scheduler loop"""
    init_db()
    # Do not preload or connect all accounts — ``execute_job`` connects lazily per
    # job so we do not hold SQLite Telethon session files open while the web app runs.

    global LAST_GEN_DATE
    logger.info(
        "Scheduler worker started",
        reserved_ai_account_ids=sorted(RESERVED_AI_AGENT_ACCOUNT_IDS),
        story_execution_enabled=story_execution_enabled(),
    )

    while True:
        try:
            # ORM stores naive UTC instants; compare with naive UTC "now".
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            today_utc = now.date()

            if LAST_GEN_DATE != today_utc:
                # Per-profile local calendar day is resolved inside the generator.
                created = generate_jobs_for_date(None)
                LAST_GEN_DATE = today_utc
                logger.info(
                    "Generator cycle complete",
                    date=str(today_utc),
                    jobs_inserted=created,
                    production_no_go=True,
                )

            claimed_ids: list[int] = []
            for _ in range(10):

                def _claim_one():
                    with get_db_context() as db:
                        return claim_due_job(
                            db,
                            worker_id=WORKER_ID,
                            lease_seconds=LEASE_SECONDS,
                            now_naive=now,
                        )

                cr = run_with_sqlite_lock_retry(
                    _claim_one,
                    operation="scheduler_claim_due_job",
                )
                if cr.job_id is None:
                    break
                claimed_ids.append(int(cr.job_id))

            for jid in claimed_ids:
                try:
                    await execute_job(jid)
                except Exception as e:
                    logger.error("Job execution failed", job_id=jid, error=str(e))
                await asyncio.sleep(2)

            # AutoStory retirement (2026-09-03): this loop no longer ticks Auto
            # Story campaigns at all -- see docs/AUTOSTORY_RETIRED_20260903.md.
            # Everything above (job generation, claim, execute) is the general
            # Scheduler product and is unaffected.

        except Exception as e:
            logger.error("Scheduler loop error", error=str(e))

        await asyncio.sleep(LOOP_INTERVAL_SEC)


def main():
    asyncio.run(run_scheduler_loop())


if __name__ == "__main__":
    main()
