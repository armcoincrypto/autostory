#!/usr/bin/env python3
"""
Run a scheduled job immediately in an isolated process.
Used by the dashboard run-now API to avoid asyncio event loop conflicts
with Telethon (which fails when reusing clients across different loops).

Usage: python -m scripts.run_job_now JOB_ID
Exit: 0 on success, 1 on failure
"""
import asyncio
import sys
import os

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.scheduler.executor import execute_job


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m scripts.run_job_now JOB_ID", file=sys.stderr)
        sys.exit(1)
    try:
        job_id = int(sys.argv[1])
    except ValueError:
        print("Invalid job_id", file=sys.stderr)
        sys.exit(1)
    ok = asyncio.run(execute_job(job_id))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
