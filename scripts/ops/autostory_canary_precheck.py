"""Read-only AutoStory canary precheck report.

Prints everything needed to decide "is it safe to create today's canary"
without requiring manual SQL. Never publishes, never mutates, never creates
a campaign, never forces a Telegram precheck -- it only reads current DB
state and calls preview_schedule() (which itself performs no provider calls,
no DB writes; see src/stories/auto_story_service.preview_schedule).

Usage (on the production host, same convention as the other ops scripts --
run from the current release directory with the real env loaded):

  cd /opt/autostory-releases/current
  set -a; source /opt/autostory/.env; source /etc/autostory/telegram-session-keys.env; set +a
  /opt/autostory/venv/bin/python - < scripts/ops/autostory_canary_precheck.py

Or with overrides for the account/window/campaign shape:

  AUTOSTORY_PRECHECK_ACCOUNT_ID=121 \
  AUTOSTORY_PRECHECK_SPAD=2 \
  AUTOSTORY_PRECHECK_WINDOW_START=08:00 \
  AUTOSTORY_PRECHECK_WINDOW_END=20:00 \
  AUTOSTORY_PRECHECK_MEDIA_PATH=/opt/autostory/data/media/canary_107_story_1080x1920.jpg \
      /opt/autostory/venv/bin/python - < scripts/ops/autostory_canary_precheck.py
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

ACCOUNT_ID = int(os.environ.get("AUTOSTORY_PRECHECK_ACCOUNT_ID") or "121")
SPAD = int(os.environ.get("AUTOSTORY_PRECHECK_SPAD") or "2")
DURATION_DAYS = int(os.environ.get("AUTOSTORY_PRECHECK_DURATION_DAYS") or "1")
WINDOW_START = os.environ.get("AUTOSTORY_PRECHECK_WINDOW_START") or "08:00"
WINDOW_END = os.environ.get("AUTOSTORY_PRECHECK_WINDOW_END") or "20:00"
MEDIA_PATH = (
    os.environ.get("AUTOSTORY_PRECHECK_MEDIA_PATH")
    or "/opt/autostory/data/media/canary_107_story_1080x1920.jpg"
)
MIN_MARGIN_MINUTES = int(os.environ.get("AUTOSTORY_PRECHECK_MIN_MARGIN_MINUTES") or "20")


def _hr(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> int:
    from src.core.database import get_db_context
    from src.core.models import Account, AutoStoryCampaign

    now = datetime.now(timezone.utc)
    _hr("DATE / TIME")
    print(f"utc_now={now.isoformat()}")
    try:
        from zoneinfo import ZoneInfo

        yerevan = now.astimezone(ZoneInfo("Asia/Yerevan"))
        print(f"yerevan_now={yerevan.isoformat()}")
    except Exception as exc:  # pragma: no cover - defensive only
        print(f"yerevan_now=UNAVAILABLE ({exc})")

    _hr("RELEASE")
    release_dir = os.getcwd()
    print(f"cwd={release_dir}")
    manifest_path = os.path.join(release_dir, "RELEASE_MANIFEST.json")
    if os.path.isfile(manifest_path):
        try:
            manifest = json.loads(open(manifest_path).read())
            print(f"git_sha={manifest.get('git_sha')}")
            print(f"release_path={manifest.get('release_path') or release_dir}")
        except Exception as exc:
            print(f"RELEASE_MANIFEST.json unreadable: {exc}")
    else:
        print("RELEASE_MANIFEST.json not found in cwd (expected if run from a release dir)")

    ok = True
    with get_db_context() as db:
        _hr("ACTIVE / PAUSED CAMPAIGNS")
        rows = (
            db.query(AutoStoryCampaign)
            .filter(AutoStoryCampaign.status.in_(["active", "paused"]))
            .all()
        )
        for c in rows:
            print(f"id={c.id} status={c.status} account_ids={c.account_ids}")
        print(f"active_or_paused_count={len(rows)}")
        if rows:
            ok = False
            print("BLOCKER: an AutoStory campaign is already active/paused -- do not create another.")

        _hr("LOCKS / CLAIMS")
        try:
            from sqlalchemy import text

            lock_count = db.execute(text("SELECT COUNT(*) FROM auto_story_account_locks")).scalar()
        except Exception as exc:
            lock_count = f"UNAVAILABLE ({exc})"
        print(f"account_locks={lock_count}")
        claimed = (
            db.query(AutoStoryCampaign)
            .filter(AutoStoryCampaign.claimed_by.isnot(None))
            .count()
        )
        print(f"campaigns_with_claim={claimed}")
        if isinstance(lock_count, int) and lock_count:
            ok = False
        if claimed:
            ok = False

        _hr("COUNTERS")
        from src.core.models import Story

        try:
            from sqlalchemy import text as _text

            msg_count = db.execute(_text("SELECT COUNT(*) FROM message_deliveries")).scalar()
        except Exception as exc:
            msg_count = f"UNAVAILABLE ({exc})"
        story_count = db.query(Story).count()
        print(f"STORIES_BEFORE={story_count}")
        print(f"MESSAGES_BEFORE={msg_count}")

        _hr(f"ACCOUNT {ACCOUNT_ID} HEALTH")
        acc = db.get(Account, ACCOUNT_ID)
        if acc is None:
            print(f"BLOCKER: account {ACCOUNT_ID} not found")
            ok = False
        else:
            status = getattr(acc.status, "value", acc.status)
            print(f"status={status}")
            print(f"story_precheck_status={acc.story_precheck_status}")
            print(f"story_precheck_reason={acc.story_precheck_reason}")
            print(f"story_precheck_checked_at={acc.story_precheck_checked_at}")
            print(f"story_status={acc.story_status}")
            print(f"story_blocked_until={acc.story_blocked_until}")
            blocked_until_expired = None
            if acc.story_blocked_until is not None:
                bu = acc.story_blocked_until
                if bu.tzinfo is None:
                    bu = bu.replace(tzinfo=timezone.utc)
                blocked_until_expired = bu <= now
                print(f"story_blocked_until_expired={blocked_until_expired}")
                if blocked_until_expired is False:
                    ok = False
                    print("BLOCKER: story_blocked_until is still in the future.")
            else:
                print("story_blocked_until_expired=N/A (never set)")
            print(f"stories_today={acc.stories_today}")
            print(f"stories_today_on={acc.stories_today_on}")

            try:
                from sqlalchemy import text as _text2

                lock_row = db.execute(
                    _text2("SELECT 1 FROM auto_story_account_locks WHERE account_id = :aid"),
                    {"aid": ACCOUNT_ID},
                ).fetchone()
            except Exception:
                lock_row = None
            print(f"account_locked={bool(lock_row)}")
            if lock_row:
                ok = False

            last_story = (
                db.query(Story)
                .filter(Story.account_id == ACCOUNT_ID, Story.is_deleted.is_(False))
                .order_by(Story.published_at.desc())
                .first()
            )
            if last_story is not None:
                print(f"last_story_published_at={last_story.published_at}")
                print(f"last_story_telegram_id={last_story.story_id}")
            else:
                print("last_story_published_at=NONE")

    _hr("PREVIEW (preview_schedule -- no provider calls, no DB writes)")
    try:
        from src.stories.auto_story_service import preview_schedule

        payload = {
            "account_ids": [ACCOUNT_ID],
            "selection_mode": "manual",
            "media_path": MEDIA_PATH,
            "caption": "Storyfleet 2/day canary",
            "campaign_mode": "recurring_daily",
            "stories_per_account_per_day": SPAD,
            "duration_days": DURATION_DAYS,
            "awake_start_hhmm": WINDOW_START,
            "awake_end_hhmm": WINDOW_END,
            "mentions_per_story": 0,
        }
        preview = preview_schedule(
            duration_days=DURATION_DAYS, posts_per_day=SPAD, times_json=None, payload=payload
        )
        print(f"accounts={preview.get('account_count')}")
        print(f"stories_per_account_per_day={preview.get('stories_per_account_per_day')}")
        print(f"duration_days={preview.get('duration_days')}")
        print(f"max_story_publishes={preview.get('max_story_publishes')}")
        print(f"mentions_off={preview.get('mentions_off')}")
        print(f"approval_blocked={preview.get('approval_blocked')}")
        print(f"media_ok={preview.get('media_ok')}")
        exec_times = preview.get("execution_times") or []
        if len(exec_times) >= 1:
            print(f"SLOT_1={exec_times[0].get('utc_iso')} ({exec_times[0].get('local_label')})")
        if len(exec_times) >= 2:
            print(f"SLOT_2={exec_times[1].get('utc_iso')} ({exec_times[1].get('local_label')})")
            t1 = datetime.fromisoformat(exec_times[0]["utc_iso"].replace("Z", "+00:00"))
            t2 = datetime.fromisoformat(exec_times[1]["utc_iso"].replace("Z", "+00:00"))
            spacing = t2 - t1
            print(f"ACTUAL_SPACING={spacing}")
            margin1 = (t1 - now).total_seconds() / 60
            margin2 = (t2 - now).total_seconds() / 60
            print(f"slot_1_margin_minutes={margin1:.1f}")
            print(f"slot_2_margin_minutes={margin2:.1f}")
            if margin1 < MIN_MARGIN_MINUTES:
                ok = False
                print(f"BLOCKER: Slot 1 is less than {MIN_MARGIN_MINUTES} minutes away.")
            if margin2 <= margin1:
                ok = False
                print("BLOCKER: Slot 2 is not after Slot 1.")
        else:
            ok = False
            print(f"BLOCKER: preview did not generate 2 execution slots (got {len(exec_times)}).")
        if not preview.get("media_ok"):
            ok = False
        if preview.get("approval_blocked"):
            ok = False
    except Exception as exc:
        ok = False
        print(f"BLOCKER: preview_schedule() raised: {type(exc).__name__}: {exc}")

    _hr("VERDICT")
    print("AUTOSTORY_CANARY_PRECHECK_OK" if ok else "AUTOSTORY_CANARY_PRECHECK_BLOCKED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
