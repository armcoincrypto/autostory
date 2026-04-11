"""
Scheduler worker - main loop: generate jobs, execute due jobs
"""
import asyncio
import json
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

# Automatically run story precheck for candidates every this many hours.
# Respects the server-side 20/hour rate limit — each trigger processes one batch.
STORY_PRECHECK_INTERVAL_HOURS = 1
_last_story_precheck_trigger: datetime = None


def _api_post(path: str, body: bytes = b"", extra_headers: dict = None) -> dict:
    """POST to the local web API. Returns parsed JSON or raises."""
    token = os.environ.get("DASHBOARD_ADMIN_TOKEN", "")
    port = int(os.environ.get("PORT", 8000))
    url = f"http://127.0.0.1:{port}{path}"
    headers = {"X-Admin-Token": token, "Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def _api_get(path: str) -> dict:
    """GET from the local web API. Returns parsed JSON or raises."""
    token = os.environ.get("DASHBOARD_ADMIN_TOKEN", "")
    port = int(os.environ.get("PORT", 8000))
    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, headers={"X-Admin-Token": token})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def _trigger_fleet_health_check() -> None:
    """
    Ask the web process to run a fleet health check by POSTing to its internal
    API. Runs in the scheduler process (separate from Gunicorn). Fails silently
    so a web hiccup never crashes the scheduler loop.
    """
    global _last_health_check_trigger
    try:
        data = _api_post("/api/accounts/healthcheck/start", body=b"{}")
        _last_health_check_trigger = datetime.utcnow()
        logger.info("Fleet health check triggered by scheduler", total=data.get("total"))
    except Exception as e:
        err = str(e)
        if "429" in err:
            _last_health_check_trigger = datetime.utcnow()
            logger.info("Fleet health check rate limited, backing off", hours=HEALTH_CHECK_INTERVAL_HOURS)
        else:
            logger.warning("Could not trigger fleet health check", error=err)


def _trigger_story_precheck() -> None:
    """
    Fetch story-precheck candidates and run one batch (up to the server's
    hourly cap). Fails silently so a web hiccup never crashes the scheduler loop.
    """
    global _last_story_precheck_trigger
    try:
        candidates_data = _api_get("/api/accounts/story-precheck-candidates")
        # live server returns account_ids (list of ints) + count
        candidates = (candidates_data.get("account_ids")
                      or candidates_data.get("candidates")
                      or [])
        count = candidates_data.get("count") or candidates_data.get("total") or len(candidates)

        if not candidates or candidates_data.get("nothing_to_run"):
            logger.info("Story precheck: no candidates, all accounts are fresh", total=count)
            _last_story_precheck_trigger = datetime.utcnow()
            return

        # Use at most 20 per batch (server-side hourly cap)
        batch_size = 20
        batch = candidates[:batch_size]
        # candidates may be ints (account IDs) or dicts with "id"
        ids = [a["id"] if isinstance(a, dict) else a for a in batch]
        body = json.dumps({"account_ids": ids, "canary_batch_ok": True}).encode()
        result = _api_post("/api/accounts/story-precheck", body=body)

        _last_story_precheck_trigger = datetime.utcnow()
        logger.info(
            "Auto story precheck batch complete",
            processed=result.get("processed", 0),
            allowed=result.get("allowed", 0),
            remaining_candidates=len(candidates) - len(batch),
        )
    except Exception as e:
        err = str(e)
        if "429" in err:
            _last_story_precheck_trigger = datetime.utcnow()
            logger.info("Story precheck rate limited, backing off", hours=STORY_PRECHECK_INTERVAL_HOURS)
        else:
            logger.warning("Could not trigger story precheck", error=err)


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

            # Periodic story precheck — automatically clear the precheck backlog
            if _last_story_precheck_trigger is None or \
                    now - _last_story_precheck_trigger > timedelta(hours=STORY_PRECHECK_INTERVAL_HOURS):
                _trigger_story_precheck()

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
