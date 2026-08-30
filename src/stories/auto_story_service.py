"""Auto Story campaigns: shared media/caption, unique mentions, multi-day waves.

Manual-first: operator creates/starts a campaign and runs the first wave via
``run_now``. Remaining waves fire from ``tick_due_auto_story_campaigns`` when
``SCHEDULER_STORY_EXECUTION_ENABLED`` is true and mutations stay allowlisted.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from src.core.database import get_db_context
from src.core.models import AutoStoryCampaign
from src.stories.rotation_audit import (
    build_story_dry_run_plan,
    live_confirmation_token_for_accounts,
    mutation_allowlist_account_ids,
)
from src.stories.scheduler_integration import (
    controlled_story_accounts_execution_allowed,
    scheduler_story_execution_enabled,
)

logger = structlog.get_logger(__name__)

AWAKE_START_MINUTES = 10 * 60  # 10:00
AWAKE_END_MINUTES = 20 * 60  # 20:00

# Backoff applied to a whole-wave fresh-story-auth failure (e.g. every selected
# account currently rate_limited/blocked/frozen). Matches the existing
# BLOCKED_POLICY backoff for consistency. Without this, next_wave_at stays due
# and the scheduler's 45s base loop (LOOP_INTERVAL_SEC) re-claims and re-runs
# a fresh CanSendStoryRequest every tick -- confirmed in production (Campaign
# #18, 2026-08-29) to repeat 100+ times over ~90 minutes for one rate-limited
# account before ends_at forced completion.
FRESH_AUTH_FAILURE_BACKOFF_MINUTES = int(
    os.environ.get("AUTOSTORY_FRESH_AUTH_FAILURE_BACKOFF_MINUTES") or "15"
)


def _record_account_deferred_if_new_state(
    db,
    *,
    campaign_id: int,
    wave_index: int,
    account_id: int,
    error: str,
    reason: str,
    blocked_until: str | None,
    retry_at: str,
) -> bool:
    """Durable autostory.account.deferred row, but only on a genuine state
    transition (new error/blocked_until for this campaign+account), not once
    per retry. Returns True if a row was written.
    """
    from src.core.models import SystemLog
    from src.stories.autostory_operator_preview import emit_autostory_system_log

    last = (
        db.query(SystemLog)
        .filter(
            SystemLog.message == "autostory.account.deferred",
            SystemLog.account_id == account_id,
        )
        .order_by(SystemLog.id.desc())
        .first()
    )
    last_details = (last.details if last is not None else None) or {}
    if (
        last is not None
        and int(last_details.get("campaign_id") or -1) == int(campaign_id)
        and last_details.get("error") == error
        and last_details.get("blocked_until") == blocked_until
    ):
        return False  # unchanged cooldown/error -- no new information to record

    emit_autostory_system_log(
        db,
        "autostory.account.deferred",
        level="WARNING",
        campaign_id=int(campaign_id),
        wave_number=int(wave_index) + 1,
        account_id=int(account_id),
        result="deferred",
        error=error,
        reason=reason or "fresh_story_auth_failed",
        blocked_until=blocked_until,
        retry_at=retry_at,
    )
    return True


def _run_coro_sync(coro):  # type: ignore[no-untyped-def]
    """Run ``coro`` from sync code, including when a loop is already running.

    The scheduler worker awaits ``maybe_tick_story_rotation`` on a live loop, so
    ``asyncio.run`` inside ``execute_wave`` must not be used in that path.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()



def compute_daily_times(posts_per_day: int) -> list[str]:
    """Spread N posts evenly across 10:00–20:00 local server clock."""
    n = max(1, min(24, int(posts_per_day or 1)))
    span = AWAKE_END_MINUTES - AWAKE_START_MINUTES
    if n == 1:
        mid = AWAKE_START_MINUTES + span // 2
        return [_minutes_to_hhmm(mid)]
    times: list[str] = []
    for i in range(n):
        # Inclusive endpoints: i=0 → start, i=n-1 → end
        mins = AWAKE_START_MINUTES + int(round(span * i / (n - 1)))
        times.append(_minutes_to_hhmm(mins))
    # Dedupe while preserving order (edge case n large)
    out: list[str] = []
    seen: set[str] = set()
    for t in times:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _minutes_to_hhmm(total: int) -> str:
    total = max(0, min(24 * 60 - 1, int(total)))
    return f"{total // 60:02d}:{total % 60:02d}"


def _parse_hhmm(value: str) -> tuple[int, int]:
    parts = str(value).strip().split(":")
    return int(parts[0]), int(parts[1])


def next_wave_after(
    *,
    now: datetime,
    times: list[str],
    ends_at: datetime | None,
) -> datetime | None:
    """Next wall-clock slot strictly after ``now``, or None if past campaign end."""
    if ends_at is not None and now >= ends_at:
        return None
    sorted_times = sorted(times or compute_daily_times(1))
    if not sorted_times:
        sorted_times = compute_daily_times(1)

    # Search today then forward up to 2 days past ends_at bound
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for _ in range(400):
        for t in sorted_times:
            hh, mm = _parse_hhmm(t)
            candidate = day.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if candidate <= now:
                continue
            if ends_at is not None and candidate > ends_at:
                return None
            return candidate
        day = day + timedelta(days=1)
        if ends_at is not None and day > ends_at + timedelta(days=1):
            return None
    return None


def campaign_presentation_status(c: AutoStoryCampaign | dict[str, Any]) -> dict[str, Any]:
    """Operator-facing status (durable status names unchanged).

    Mapping:
    - active + current policy/media/mentions blocker → Needs Attention
    - active + claim in progress → Running
    - active + future next_wave + no claim → Scheduled
    """
    from src.stories.autostory_media import validate_campaign_execution_policy

    if isinstance(c, dict):
        status = str(c.get("status") or "")
        last_error = c.get("last_error")
        claimed_by = c.get("claimed_by")
        next_wave_at = c.get("next_wave_at")
    else:
        status = str(c.status or "")
        last_error = c.last_error
        claimed_by = getattr(c, "claimed_by", None)
        next_wave_at = getattr(c, "next_wave_at", None)

    policy = None
    policy_block = False
    if status in {"active", "paused", "draft", "scheduled", "approved"}:
        try:
            policy = validate_campaign_execution_policy(c)
            policy_block = status == "active" and not bool(policy.get("ok"))
        except Exception:
            policy = None
            policy_block = False

    blocking_error = bool(last_error)
    blocking = blocking_error or policy_block
    blocking_media = str(last_error or "") in {
        "missing_or_invalid_media",
        "media_not_found",
        "media_outside_library",
        "media_path_required",
        "story_media_normalization_required",
    } or "media" in str(last_error or "").lower()
    blocking_mentions = (
        (policy and policy.get("reason") == "mentions_not_production_certified")
        or str(last_error or "") == "mentions_not_production_certified"
    )

    if status == "scheduled":
        label, tone = "Scheduled", "ok"
    elif status == "active" and blocking:
        label, tone = "Needs Attention", "warn"
    elif status == "active" and claimed_by:
        label, tone = "Running", "ok"
    elif status == "active" and next_wave_at:
        label, tone = "Scheduled", "ok"
    elif status == "active":
        label, tone = "Waiting", "ok"
    elif status == "paused":
        label, tone = "Paused", "muted"
    elif status == "completed":
        label, tone = "Completed", "ok"
    elif status == "cancelled":
        label, tone = "Cancelled", "muted"
    elif status == "failed":
        label, tone = "Failed", "err"
    elif status == "draft":
        label, tone = "Draft", "muted"
    else:
        label, tone = status.replace("_", " ").title() or "Unknown", "muted"

    detail = None
    if status == "active" and blocking_mentions:
        detail = (
            "This campaign cannot publish under the current AutoStory policy. "
            "Mentions are not production-certified."
        )
    elif status == "active" and policy_block and policy:
        detail = policy.get("message") or "This campaign cannot publish under the current AutoStory policy."
    elif status == "active" and blocking_media:
        detail = "Publishing paused — media is unavailable or not Story-compatible."
    elif status == "active" and blocking_error:
        detail = f"Publishing paused — {last_error}."

    return {
        "durable_status": status,
        "label": label,
        "tone": tone,
        "needs_attention": status == "active" and blocking,
        "detail": detail,
        "last_error": last_error,
        "policy": {
            "ok": None if policy is None else bool(policy.get("ok")),
            "classification": None if policy is None else policy.get("classification"),
            "reason": None if policy is None else policy.get("reason"),
        },
    }


def campaign_to_dict(c: AutoStoryCampaign) -> dict[str, Any]:
    presentation = campaign_presentation_status(c)
    from src.stories.autostory_recurring import is_recurring, max_story_publishes

    account_ids = list(c.account_ids or [])
    spad = int(getattr(c, "stories_per_account_per_day", None) or 1)
    duration = int(c.duration_days or 1)
    mode = str(getattr(c, "campaign_mode", None) or "accounts_publish_once")
    if is_recurring(c):
        max_pub = max_story_publishes(len(account_ids), spad, duration)
    else:
        max_pub = len(account_ids)

    return {
        "id": int(c.id),
        "status": c.status,
        "presentation_status": presentation["label"],
        "presentation": presentation,
        "account_ids": account_ids,
        "media_path": c.media_path,
        "caption": c.caption or "",
        "mention_source_chat_id": c.mention_source_chat_id,
        "mentions_per_story": int(c.mentions_per_story or 0),
        "duration_days": duration,
        "posts_per_day": int(c.posts_per_day or 1),
        "times_json": list(c.times_json or []),
        "campaign_mode": mode,
        "stories_per_account_per_day": spad,
        "awake_start_hhmm": getattr(c, "awake_start_hhmm", None) or "10:00",
        "awake_end_hhmm": getattr(c, "awake_end_hhmm", None) or "20:00",
        "max_story_publishes": max_pub,
        "started_at": c.started_at.isoformat() if c.started_at else None,
        "ends_at": c.ends_at.isoformat() if c.ends_at else None,
        "next_wave_at": c.next_wave_at.isoformat() if c.next_wave_at else None,
        "last_wave_at": c.last_wave_at.isoformat() if c.last_wave_at else None,
        "last_story_run_id": c.last_story_run_id,
        "waves_ok": int(c.waves_ok or 0),
        "waves_failed": int(c.waves_failed or 0),
        "last_error": c.last_error,
        "claimed_by": getattr(c, "claimed_by", None),
        "claimed_at": c.claimed_at.isoformat() if getattr(c, "claimed_at", None) else None,
        "claim_expires_at": (
            c.claim_expires_at.isoformat() if getattr(c, "claim_expires_at", None) else None
        ),
        "authorized_account_ids": list(getattr(c, "authorized_account_ids", None) or []),
        "authorization_wave_index": getattr(c, "authorization_wave_index", None),
        "authorization_revoked_at": (
            c.authorization_revoked_at.isoformat()
            if getattr(c, "authorization_revoked_at", None)
            else None
        ),
        "confirmation_token_expected": live_confirmation_token_for_accounts(
            list(c.account_ids or [])
        ),
        "max_autostory_wave_size": 25,
    }


def preview_schedule(
    *,
    duration_days: int,
    posts_per_day: int,
    start_at: datetime | None = None,
    times_json: Any = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Canonical schedule preview — honors explicit ``times_json`` (never substitutes awake defaults)."""
    from src.stories.autostory_operator_preview import (
        build_operator_campaign_preview,
        format_execution_times,
        normalize_times_json,
        operator_timezone_name,
    )

    body = dict(payload or {})
    body.setdefault("duration_days", duration_days)
    body.setdefault("posts_per_day", posts_per_day)
    if times_json is not None:
        body["times_json"] = times_json
    if start_at is not None:
        body["_preview_start_at"] = start_at

    try:
        with get_db_context() as db:
            return build_operator_campaign_preview(db, body)
    except Exception as exc:
        logger.warning("autostory_preview_db_fallback", error=str(exc))
        sched = normalize_times_json(body.get("times_json"), posts_per_day=int(body.get("posts_per_day") or 1))
        times = list(sched["times_json"])
        duration = max(1, int(body.get("duration_days") or 1))
        now = start_at or datetime.utcnow()
        ends = now + timedelta(days=duration)
        first = next_wave_after(now=now - timedelta(seconds=1), times=times, ends_at=ends)
        return {
            "ok": True,
            "schedule_mode": sched["schedule_mode"],
            "explicit_times": bool(sched["explicit"]),
            "times_json": times,
            "duration_days": duration,
            "posts_per_day": max(1, len(times) if sched["explicit"] else int(body.get("posts_per_day") or 1)),
            "awake_window": sched.get("awake_window"),
            "timezone": operator_timezone_name(),
            "ends_at": ends.isoformat() + "Z",
            "first_wave_at": (first.isoformat() + "Z") if first else None,
            "execution_times": format_execution_times(
                times, start_at=now, ends_at=ends, duration_days=duration
            ),
            "max_autostory_wave_size": 25,
        }


def preview_schedule_legacy_awake(
    *,
    duration_days: int,
    posts_per_day: int,
    start_at: datetime | None = None,
) -> dict[str, Any]:
    """Awake-window-only preview (explicit times must use ``preview_schedule`` / operator preview)."""
    now = start_at or datetime.utcnow()
    times = compute_daily_times(posts_per_day)
    ends = now + timedelta(days=max(1, int(duration_days or 1)))
    first = next_wave_after(now=now - timedelta(seconds=1), times=times, ends_at=ends)
    return {
        "schedule_mode": "awake_window",
        "explicit_times": False,
        "times_json": times,
        "duration_days": max(1, int(duration_days or 1)),
        "posts_per_day": max(1, int(posts_per_day or 1)),
        "ends_at": ends.isoformat() + "Z",
        "first_wave_at": (first.isoformat() + "Z") if first else None,
        "awake_window": "10:00-20:00",
    }


def _normalize_account_ids(raw: Any) -> list[int]:
    if raw is None:
        return []
    if isinstance(raw, list):
        vals = raw
    else:
        vals = [raw]
    out: list[int] = []
    seen: set[int] = set()
    for x in vals:
        if x is None or x == "":
            continue
        aid = int(x)
        if aid in seen:
            continue
        seen.add(aid)
        out.append(aid)
    return out


def create_campaign(payload: dict[str, Any]) -> dict[str, Any]:
    from src.stories.autostory_operator_preview import (
        build_operator_campaign_preview,
        normalize_times_json,
        select_automatic_accounts,
    )
    from src.stories.autostory_media import normalize_campaign_mentions, validate_campaign_media
    from src.stories.autostory_recurring import (
        CAMPAIGN_MODE_ONCE,
        CAMPAIGN_MODE_RECURRING,
        awake_bounds,
        campaign_local_dates,
        clamp_stories_per_account_per_day,
        ensure_daily_progress_rows,
        plan_recurring_times_json,
        recurring_campaign_ends_at,
    )

    media_raw = str(payload.get("media_path") or "").strip()
    if not media_raw:
        return {"ok": False, "error": "media_path_required"}

    media_check = validate_campaign_media(media_raw)
    media_path = str(media_check.get("path") or media_raw)
    if not media_check.get("ok"):
        return {
            "ok": False,
            "error": media_check.get("error") or "missing_or_invalid_media",
            "message": media_check.get("message") or "Media is missing or not Story-compatible.",
            "media_validation": media_check,
        }

    duration_days = max(1, min(90, int(payload.get("duration_days") or 1)))
    # New creates default to recurring_daily unless explicitly legacy
    raw_mode = str(payload.get("campaign_mode") or CAMPAIGN_MODE_RECURRING).strip().lower()
    if payload.get("legacy_once") is True:
        raw_mode = CAMPAIGN_MODE_ONCE
    if raw_mode not in {CAMPAIGN_MODE_ONCE, CAMPAIGN_MODE_RECURRING}:
        raw_mode = CAMPAIGN_MODE_RECURRING
    spad = clamp_stories_per_account_per_day(payload.get("stories_per_account_per_day", 1))
    start_hhmm, end_hhmm = awake_bounds(
        {
            "awake_start_hhmm": payload.get("awake_start_hhmm") or payload.get("window_start"),
            "awake_end_hhmm": payload.get("awake_end_hhmm") or payload.get("window_end"),
        }
    )

    posts_per_day = max(1, min(12, int(payload.get("posts_per_day") or 1)))
    if raw_mode == CAMPAIGN_MODE_RECURRING and not payload.get("times_json"):
        # Placeholder; finalize after account selection
        sched = {"explicit": False, "times_json": [], "schedule_mode": "awake_window"}
        times: list[str] = []
    else:
        sched = normalize_times_json(payload.get("times_json"), posts_per_day=posts_per_day)
        times = [str(t) for t in sched["times_json"]]
        if sched["explicit"]:
            posts_per_day = max(1, len(times))

    # Optional absolute first fire (schedule-once ISO); engine still stores HH:MM
    force_next: datetime | None = None
    raw_next = payload.get("next_wave_at") or payload.get("schedule_at")
    if raw_next:
        try:
            iso = str(raw_next).replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            force_next = dt
        except ValueError:
            force_next = None

    msc = payload.get("mention_source_chat_id", payload.get("mention_source"))
    mention_source = int(msc) if msc not in (None, "", "null") else None
    mention_norm = normalize_campaign_mentions(payload.get("mentions_per_story", 0))
    mentions_per_story = int(mention_norm["mentions_per_story"])
    caption = payload.get("caption") or ""

    activate = bool(payload.get("activate") or payload.get("start"))
    confirmation = str(payload.get("confirmation_token") or "").strip()
    approval = payload.get("explicit_operator_approval") is True
    # Operator UX: preview_reviewed maps to canonical multi-account confirmation token
    if payload.get("preview_reviewed") is True and not confirmation:
        confirmation = "I_CONFIRM_STORY_PUBLISH"
        approval = True
    acknowledge_shortfall = bool(
        payload.get("acknowledge_eligibility_shortfall") or payload.get("allow_fewer")
    )

    if activate and mention_norm.get("mentions_forced_off"):
        return {
            "ok": False,
            "error": "mentions_not_production_certified",
            "message": mention_norm.get("message")
            or "Mentions are not yet production-certified for AutoStory.",
        }

    now = datetime.utcnow()
    with get_db_context() as db:
        selection_mode = str(payload.get("selection_mode") or "manual").strip().lower()
        account_ids = _normalize_account_ids(payload.get("account_ids") or payload.get("account_id"))
        auto_meta = None
        if selection_mode == "automatic" or (
            not account_ids and payload.get("automatic_count") is not None
        ):
            auto_meta = select_automatic_accounts(
                db,
                requested=payload.get("automatic_count")
                if payload.get("automatic_count") is not None
                else payload.get("requested_count") or 0,
            )
            if auto_meta.get("acknowledgment_required") and not acknowledge_shortfall:
                return {
                    "ok": False,
                    "error": "eligibility_shortfall",
                    "message": auto_meta.get("message"),
                    "automatic": auto_meta,
                }
            account_ids = list(auto_meta["selected_account_ids"])
            selection_mode = "automatic"

        if not account_ids:
            return {"ok": False, "error": "account_ids_required"}

        if raw_mode == CAMPAIGN_MODE_RECURRING and not payload.get("times_json"):
            planned_sched = plan_recurring_times_json(
                account_count=len(account_ids),
                stories_per_account_per_day=spad,
                awake_start=start_hhmm,
                awake_end=end_hhmm,
            )
            times = [str(t) for t in planned_sched["times_json"]]
            posts_per_day = int(planned_sched.get("pickup_opportunities") or 1)
            sched = planned_sched

        preview_payload = {
            **payload,
            "account_ids": account_ids,
            "duration_days": duration_days,
            "posts_per_day": posts_per_day,
            "selection_mode": selection_mode,
            "mentions_per_story": mentions_per_story,
            "media_path": media_path,
            "caption": caption,
            "campaign_mode": raw_mode,
            "stories_per_account_per_day": spad,
            "awake_start_hhmm": start_hhmm,
            "awake_end_hhmm": end_hhmm,
            "times_json": times if sched.get("explicit") else None,
        }
        preview = build_operator_campaign_preview(db, preview_payload)

        if activate and (preview.get("approval_blocked") or not preview.get("can_approve")):
            return {
                "ok": False,
                "error": "approval_blocked",
                "message": preview.get("approval_block_reason") or "Campaign cannot be approved.",
                "preview": preview,
            }

        camp = AutoStoryCampaign(
            status="draft",
            account_ids=account_ids,
            media_path=media_path,
            caption=caption,
            mention_source_chat_id=mention_source,
            mentions_per_story=mentions_per_story,
            duration_days=duration_days,
            posts_per_day=posts_per_day,
            times_json=times,
            campaign_mode=raw_mode,
            stories_per_account_per_day=spad,
            awake_start_hhmm=start_hhmm,
            awake_end_hhmm=end_hhmm,
            explicit_operator_approval=False,
            confirmation_token=None,
        )
        if activate:
            if not approval or not confirmation:
                return {
                    "ok": False,
                    "error": "operator_approval_required",
                    "message": "Start requires explicit_operator_approval and confirmation_token.",
                    "confirmation_token_expected": live_confirmation_token_for_accounts(account_ids),
                    "preview": preview,
                }
            expected = live_confirmation_token_for_accounts(account_ids)
            if confirmation not in {expected, "I_CONFIRM_STORY_PUBLISH"}:
                return {
                    "ok": False,
                    "error": "confirmation_token_required",
                    "confirmation_token_expected": expected,
                    "preview": preview,
                }
            camp.status = "active"
            camp.started_at = now
            if raw_mode == CAMPAIGN_MODE_RECURRING:
                camp.ends_at = recurring_campaign_ends_at(
                    started_at=now, duration_days=duration_days
                )
            else:
                camp.ends_at = now + timedelta(days=duration_days)
            computed = next_wave_after(
                now=now - timedelta(seconds=1),
                times=times,
                ends_at=camp.ends_at,
            )
            if force_next is not None and force_next > now:
                camp.next_wave_at = force_next
                if camp.ends_at and force_next > camp.ends_at:
                    camp.ends_at = force_next + timedelta(hours=1)
            else:
                camp.next_wave_at = computed
            camp.explicit_operator_approval = True
            camp.confirmation_token = confirmation

        db.add(camp)
        db.flush()
        if activate and raw_mode == CAMPAIGN_MODE_RECURRING:
            dates = campaign_local_dates(
                started_at=camp.started_at or now,
                duration_days=duration_days,
            )
            ensure_daily_progress_rows(
                db,
                campaign_id=int(camp.id),
                account_ids=account_ids,
                local_dates=dates,
                target_count=spad,
            )
        db.commit()
        db.refresh(camp)
        out = campaign_to_dict(camp)

    return {
        "ok": True,
        "campaign": out,
        "preview": preview,
        "schedule_mode": sched["schedule_mode"],
        "explicit_times": bool(sched["explicit"]),
        "selection_mode": selection_mode,
    }


def get_active_campaigns() -> list[dict[str, Any]]:
    with get_db_context() as db:
        rows = (
            db.query(AutoStoryCampaign)
            .filter(AutoStoryCampaign.status.in_(("active", "paused", "draft")))
            .order_by(AutoStoryCampaign.id.desc())
            .limit(20)
            .all()
        )
        out = []
        for r in rows:
            d = campaign_to_dict(r)
            d["progress"] = _campaign_progress(db, r, d.get("max_story_publishes") or 0)
            out.append(d)
        return out


def get_recent_campaigns(*, limit: int = 25) -> list[dict[str, Any]]:
    """Recent campaigns for operator table (incl. completed/cancelled)."""
    with get_db_context() as db:
        rows = (
            db.query(AutoStoryCampaign)
            .order_by(AutoStoryCampaign.id.desc())
            .limit(max(1, min(100, int(limit))))
            .all()
        )
        out = []
        for r in rows:
            d = campaign_to_dict(r)
            d["progress"] = _campaign_progress(db, r, d.get("max_story_publishes") or 0)
            out.append(d)
        return out


def get_campaign(campaign_id: int) -> dict[str, Any] | None:
    with get_db_context() as db:
        c = db.get(AutoStoryCampaign, int(campaign_id))
        if not c:
            return None
        out = campaign_to_dict(c)
        out["progress"] = _campaign_progress(db, c, out.get("max_story_publishes") or 0)
        return out


def _campaign_progress(db, c: AutoStoryCampaign, max_pub: int) -> dict[str, Any]:
    from src.stories.autostory_recurring import (
        campaign_current_day_number,
        campaign_daily_progress_summary,
        campaign_shortfall_reason,
        is_recurring,
    )

    status = str(getattr(c, "status", "") or "")
    if is_recurring(c):
        summary = campaign_daily_progress_summary(db, int(c.id))
        out = {
            **summary,
            "max_story_publishes": max_pub,
            "successful_count": int(summary.get("successful_count") or 0),
            "target_count": int(summary.get("target_count") or max_pub),
            "day_number": campaign_current_day_number(
                started_at=c.started_at, duration_days=int(c.duration_days or 1)
            ),
            "total_days": int(c.duration_days or 1),
        }
        # Shortfall on already-finalized days -- shown while the campaign is
        # still active (a past day fell short) as well as after it ends, but
        # never for today's still-in-progress day.
        shortfall = int(summary.get("finalized_shortfall") or 0)
        if shortfall > 0:
            out["shortfall"] = shortfall
            out["shortfall_reason"] = campaign_shortfall_reason(db, int(c.id))
        return out

    from src.core.models import AutoStoryAccountProgress

    rows = (
        db.query(AutoStoryAccountProgress)
        .filter(AutoStoryAccountProgress.campaign_id == int(c.id))
        .all()
    )
    ok_n = sum(1 for r in rows if r.status in {"reconciled", "published", "ok"})
    out = {
        "successful_count": ok_n,
        "target_count": max_pub,
        "remaining_count": max(0, max_pub - ok_n),
        "max_story_publishes": max_pub,
    }
    # Legacy accounts_publish_once has no day granularity -- only flag a
    # shortfall once the campaign has actually stopped running.
    if status in ("completed", "failed") and ok_n < max_pub:
        out["shortfall"] = max_pub - ok_n
        out["shortfall_reason"] = campaign_shortfall_reason(db, int(c.id))
    return out

def cancel_campaign(campaign_id: int) -> dict[str, Any]:
    from src.stories.autostory_hardening import (
        release_campaign_claim,
        revoke_wave_authorization,
    )

    with get_db_context() as db:
        c = db.get(AutoStoryCampaign, int(campaign_id))
        if c is None:
            return {"ok": False, "error": "not_found"}
        c.status = "cancelled"
        c.next_wave_at = None
        c.updated_at = datetime.utcnow()
        db.commit()
        revoke_wave_authorization(db, int(campaign_id))
        release_campaign_claim(db, int(campaign_id), force=True)
        db.refresh(c)
        return {"ok": True, "campaign": campaign_to_dict(c)}


def pause_campaign(campaign_id: int) -> dict[str, Any]:
    from src.stories.autostory_hardening import (
        release_campaign_claim,
        revoke_wave_authorization,
    )

    with get_db_context() as db:
        c = db.get(AutoStoryCampaign, int(campaign_id))
        if c is None:
            return {"ok": False, "error": "not_found"}
        if c.status not in ("active", "draft"):
            return {"ok": False, "error": "invalid_status", "campaign": campaign_to_dict(c)}
        c.status = "paused"
        c.updated_at = datetime.utcnow()
        db.commit()
        revoke_wave_authorization(db, int(campaign_id))
        release_campaign_claim(db, int(campaign_id), force=True)
        db.refresh(c)
        return {"ok": True, "campaign": campaign_to_dict(c)}


def activate_campaign(campaign_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    from src.stories.autostory_media import validate_campaign_media

    confirmation = str(payload.get("confirmation_token") or "").strip()
    approval = payload.get("explicit_operator_approval") is True
    if payload.get("preview_reviewed") is True and not confirmation:
        confirmation = "I_CONFIRM_STORY_PUBLISH"
        approval = True
    with get_db_context() as db:
        c = db.get(AutoStoryCampaign, int(campaign_id))
        if c is None:
            return {"ok": False, "error": "not_found"}
        media_check = validate_campaign_media(c.media_path)
        if not media_check.get("ok"):
            return {
                "ok": False,
                "error": media_check.get("error") or "missing_or_invalid_media",
                "message": media_check.get("message") or "Media is missing or not Story-compatible.",
                "media_validation": media_check,
            }
        if media_check.get("path"):
            c.media_path = str(media_check["path"])
        account_ids = list(c.account_ids or [])
        expected = live_confirmation_token_for_accounts(account_ids)
        if not approval or confirmation not in {expected, "I_CONFIRM_STORY_PUBLISH"}:
            return {
                "ok": False,
                "error": "operator_approval_required",
                "confirmation_token_expected": expected,
            }
        now = datetime.utcnow()
        c.status = "active"
        c.started_at = c.started_at or now
        if c.ends_at is None:
            from src.stories.autostory_recurring import is_recurring, recurring_campaign_ends_at

            if is_recurring(c):
                c.ends_at = recurring_campaign_ends_at(
                    started_at=c.started_at, duration_days=int(c.duration_days or 1)
                )
            else:
                c.ends_at = now + timedelta(days=int(c.duration_days or 1))
        c.next_wave_at = next_wave_after(
            now=now - timedelta(seconds=1),
            times=list(c.times_json or []),
            ends_at=c.ends_at,
        )
        c.explicit_operator_approval = True
        c.confirmation_token = confirmation
        c.last_error = None
        c.updated_at = now
        db.commit()
        db.refresh(c)
        return {"ok": True, "campaign": campaign_to_dict(c)}


def _wave_payload_from_campaign(
    c: AutoStoryCampaign,
    *,
    mention_plan: list[dict],
    account_ids: list[int] | None = None,
) -> dict[str, Any]:
    ids = list(account_ids if account_ids is not None else (c.account_ids or []))
    return {
        "account_ids": ids,
        "media_path": c.media_path,
        "caption": c.caption or "",
        "mentions_per_story": int(c.mentions_per_story or 0),
        "mention_source_chat_id": c.mention_source_chat_id,
        "mention_strategy": "random",
        "max_stories": len(ids),
        "mode": "once",
        "explicit_operator_approval": True,
        "confirmation_token": c.confirmation_token,
        "selected_mention_candidates": mention_plan,
    }


def dry_run_campaign_wave(campaign_id: int) -> dict[str, Any]:
    from src.stories.autostory_hardening import (
        MAX_AUTOSTORY_WAVE_SIZE,
        plan_full_fleet_waves,
        select_next_wave_accounts,
    )

    with get_db_context() as db:
        c = db.get(AutoStoryCampaign, int(campaign_id))
        if c is None:
            return {"ok": False, "error": "not_found"}
        wave_index = int(getattr(c, "authorization_wave_index", None) or 0) + (
            0 if getattr(c, "authorized_account_ids", None) else 0
        )
        # Preview next wave as wave 0 or current+1 semantics: use waves_ok as index
        wave_index = int(c.waves_ok or 0)
        selection = select_next_wave_accounts(db, c, wave_index=wave_index)
        account_ids = list(selection["wave_account_ids"])
        fleet_plan = plan_full_fleet_waves(db, list(c.account_ids or []))
        payload = {
            "account_ids": account_ids,
            "media_path": c.media_path,
            "caption": c.caption or "",
            "mentions_per_story": int(c.mentions_per_story or 0),
            "mention_source_chat_id": c.mention_source_chat_id,
            "mention_strategy": "random",
            "max_stories": len(account_ids),
            "dry_run": True,
        }
        plan = build_story_dry_run_plan(db, payload)
        calendar = None
        from src.stories.autostory_recurring import is_recurring, plan_recurring_dry_run_calendar

        if is_recurring(c):
            calendar = plan_recurring_dry_run_calendar(
                account_ids=list(c.account_ids or []),
                stories_per_account_per_day=int(getattr(c, "stories_per_account_per_day", None) or 1),
                duration_days=int(c.duration_days or 1),
                started_at=c.started_at,
                awake_start=getattr(c, "awake_start_hhmm", None),
                awake_end=getattr(c, "awake_end_hhmm", None),
            )
    return {
        "ok": bool(plan.get("ok")),
        "dry_run": True,
        "campaign_id": int(campaign_id),
        "plan": plan,
        "wave_preview": selection,
        "fleet_plan": fleet_plan,
        "recurring_calendar": calendar,
        "max_autostory_wave_size": MAX_AUTOSTORY_WAVE_SIZE,
        "selected_mention_candidates": plan.get("selected_mention_candidates") or [],
        "per_account_mentions": plan.get("per_account_mentions") or [],
        "confirmation_token_expected": plan.get("confirmation_token_expected"),
        "mutation_allowlist_account_ids": plan.get("mutation_allowlist_account_ids") or [],
        "live_publish_allowed": bool((plan.get("precheck") or {}).get("live_publish_allowed")),
        "would_publish": False,
        "reason": "dry_run",
        "wave_size": len(account_ids),
        "media": c.media_path,
        "caption": c.caption or "",
        "daily_target": int(getattr(c, "stories_per_account_per_day", None) or 1),
        "mentions_target": int(c.mentions_per_story or 0),
    }


def _advance_after_wave(
    c: AutoStoryCampaign,
    *,
    now: datetime,
    ok: bool,
    run_id: int | None,
    error: str | None,
    remaining_accounts: int = 0,
    continue_immediately: bool | None = None,
) -> None:
    if ok:
        c.waves_ok = int(c.waves_ok or 0) + 1
        c.last_error = None
    else:
        c.waves_failed = int(c.waves_failed or 0) + 1
        c.last_error = (error or "wave_failed")[:2000]
    c.last_wave_at = now
    if run_id is not None:
        c.last_story_run_id = int(run_id)
    # remaining_accounts==0 → campaign work finished (once or all daily targets met)
    if ok and remaining_accounts <= 0:
        c.status = "completed"
        c.next_wave_at = None
        c.updated_at = now
        return
    cont = continue_immediately if continue_immediately is not None else (remaining_accounts > 0)
    if ok and cont:
        # Immediate continuation for next bounded wave (same schedule fire)
        c.next_wave_at = now
        c.status = "active"
    else:
        nxt = next_wave_after(now=now, times=list(c.times_json or []), ends_at=c.ends_at)
        if nxt is None:
            c.status = "completed"
            c.next_wave_at = None
        else:
            c.next_wave_at = nxt
            c.status = "active"
    c.updated_at = now


def _record_wave_progress_from_result(
    db,
    *,
    campaign_id: int,
    wave_index: int,
    result: dict[str, Any],
) -> None:
    from src.core.models import AutoStoryCampaign
    from src.stories.autostory_hardening import update_progress
    from src.stories.autostory_operator_preview import emit_autostory_system_log, log_autostory_event
    from src.stories.autostory_recurring import (
        is_recurring,
        record_daily_ambiguous,
        record_daily_failed,
        record_daily_success,
    )

    camp = db.get(AutoStoryCampaign, int(campaign_id))
    recurring = bool(camp is not None and is_recurring(camp))

    run_id = result.get("run_id")
    for step in result.get("steps") or []:
        aid = int(step.get("account_id"))
        if step.get("ambiguous_no_retry") or step.get("status") == "ambiguous_no_retry":
            status = "ambiguous"
        elif step.get("ok"):
            status = "reconciled"
        else:
            status = "failed"
        update_progress(
            db,
            campaign_id=campaign_id,
            wave_index=wave_index,
            account_id=aid,
            status=status,
            story_id=step.get("db_id"),
            telegram_story_id=step.get("story_id"),
            run_id=run_id,
            error=None if step.get("ok") else str(step.get("error") or "failed"),
        )
        if recurring:
            if status == "reconciled":
                record_daily_success(
                    db,
                    campaign_id=campaign_id,
                    account_id=aid,
                    story_id=step.get("db_id"),
                )
            elif status == "ambiguous":
                record_daily_ambiguous(db, campaign_id=campaign_id, account_id=aid)
            else:
                record_daily_failed(db, campaign_id=campaign_id, account_id=aid)
        duration_ms = step.get("duration_ms")
        if status == "reconciled":
            emit_autostory_system_log(
                db,
                "autostory.account.published",
                campaign_id=campaign_id,
                wave_number=wave_index + 1,
                account_id=aid,
                result="published",
                telegram_story_id=step.get("story_id"),
                db_story_id=step.get("db_id"),
                story_run_id=run_id,
                duration_ms=duration_ms,
            )
        elif status == "failed":
            log_autostory_event(
                "autostory.account.failed",
                level="error",
                campaign_id=campaign_id,
                wave_number=wave_index + 1,
                account_id=aid,
                result="failed",
                error=str(step.get("error") or "failed")[:200],
            )
        else:
            log_autostory_event(
                "autostory.account.deferred",
                level="warning",
                campaign_id=campaign_id,
                wave_number=wave_index + 1,
                account_id=aid,
                result=status,
            )


def execute_wave(
    campaign_id: int,
    *,
    operator_manual: bool = False,
    require_scheduler_flag: bool = False,
    worker_id: str | None = None,
    already_claimed: bool = False,
) -> dict[str, Any]:
    """Publish one bounded wave (<=25) with durable claim + campaign-scoped auth."""
    from src.stories.autostory_hardening import (
        MAX_AUTOSTORY_WAVE_SIZE,
        PROGRESS_NO_RESEND,
        authorize_wave,
        claim_campaign,
        ensure_progress_row,
        is_account_certified_publish,
        load_durable_mention_plan,
        persist_mention_plan_for_wave,
        progress_status,
        release_campaign_claim,
        revoke_wave_authorization,
        select_next_wave_accounts,
        update_progress,
        wave_size_ok,
        worker_identity,
    )
    from src.stories.rotation_audit import allocate_mentions_without_replacement
    from src.stories.controlled_live_run import (
        _execute_controlled_live_story_run,
        evaluate_controlled_live_run_gates,
    )
    from src.stories.mutation_boundary import story_mutations_enabled
    from src.stories.rotation_audit import build_story_rotation_precheck

    worker_id = worker_id or worker_identity()

    if require_scheduler_flag and not scheduler_story_execution_enabled():
        return {
            "ok": False,
            "skipped": True,
            "error": "scheduler_story_execution_disabled",
            "campaign_id": int(campaign_id),
        }
    if not story_mutations_enabled():
        return {
            "ok": False,
            "skipped": True,
            "error": "story_mutations_disabled",
            "campaign_id": int(campaign_id),
        }

    claimed_here = False
    wave_index = 0
    wave_truncated = False
    camp_snapshot: dict[str, Any] = {"id": int(campaign_id)}

    try:
        with get_db_context() as db:
            c = db.get(AutoStoryCampaign, int(campaign_id))
            if c is None:
                return {"ok": False, "error": "not_found"}
            if c.status != "active":
                return {"ok": False, "error": "campaign_not_active", "campaign": campaign_to_dict(c)}
            if not c.explicit_operator_approval or not c.confirmation_token:
                return {"ok": False, "error": "operator_approval_required", "campaign": campaign_to_dict(c)}

            from src.stories.autostory_media import validate_campaign_execution_policy

            policy = validate_campaign_execution_policy(c)
            if not policy.get("ok"):
                now = datetime.utcnow()
                c.last_error = str(policy.get("reason") or policy.get("error") or "BLOCKED_POLICY")[:2000]
                c.updated_at = now
                if not operator_manual and require_scheduler_flag:
                    # Back off; do not authorize or publish
                    c.next_wave_at = now + timedelta(minutes=15)
                db.commit()
                return {
                    "ok": False,
                    "error": "BLOCKED_POLICY",
                    "reason": policy.get("reason"),
                    "classification": policy.get("classification"),
                    "message": policy.get("message"),
                    "blockers": policy.get("blockers"),
                    "campaign_id": int(campaign_id),
                    "campaign": campaign_to_dict(c),
                }

            if not already_claimed:
                won, claim_meta = claim_campaign(db, int(campaign_id), worker_id=worker_id)
                if not won:
                    return {
                        "ok": False,
                        "skipped": True,
                        "error": "campaign_claim_lost",
                        "claim": claim_meta,
                        "campaign_id": int(campaign_id),
                    }
                claimed_here = True
            else:
                # Verify we still own the claim
                if c.claimed_by and c.claimed_by != worker_id:
                    return {
                        "ok": False,
                        "skipped": True,
                        "error": "campaign_claim_lost",
                        "campaign_id": int(campaign_id),
                    }

            from src.stories.autostory_operator_preview import emit_autostory_system_log, log_autostory_event

            log_autostory_event(
                "autostory.campaign.started",
                campaign_id=int(campaign_id),
                story_run_id=c.last_story_run_id,
                worker_id=worker_id,
                account_count=len(list(c.account_ids or [])),
                wave_index=int(c.waves_ok or 0),
            )

            wave_index = int(c.waves_ok or 0)
            selection = select_next_wave_accounts(db, c, wave_index=wave_index)
            account_ids = list(selection["wave_account_ids"])

            # Resume filter: skip durable terminal states for this wave
            runnable: list[int] = []
            for aid in account_ids:
                st = progress_status(db, campaign_id=int(campaign_id), wave_index=wave_index, account_id=aid)
                if st in PROGRESS_NO_RESEND:
                    continue
                if st == "ambiguous":
                    continue
                runnable.append(aid)
            account_ids = runnable

            ok_size, size_err = wave_size_ok(account_ids)
            if not ok_size:
                revoke_wave_authorization(db, int(campaign_id))
                release_campaign_claim(db, int(campaign_id), worker_id=worker_id, force=True)
                return {
                    "ok": False,
                    "error": size_err or "WAVE_SIZE_EXCEEDS_MAX",
                    "campaign_id": int(campaign_id),
                    "account_count": len(account_ids),
                    "max_autostory_wave_size": MAX_AUTOSTORY_WAVE_SIZE,
                }

            if not account_ids:
                # Capacity blocked, done for today, or all campaign targets met
                unfinished = int(selection.get("unfinished_count") or 0)
                today_rem = int(selection.get("today_remaining_accounts") or 0)
                cap_blocked = int(selection.get("capacity_blocked_today") or 0)
                now = datetime.utcnow()
                from src.stories.autostory_recurring import is_recurring, recurring_campaign_complete

                if unfinished == 0 or (
                    is_recurring(c) and recurring_campaign_complete(db, c)
                ):
                    c.status = "completed"
                    c.next_wave_at = None
                    c.last_error = None
                    emit_autostory_system_log(
                        db,
                        "autostory.campaign.completed",
                        campaign_id=int(campaign_id),
                        successful=int(c.waves_ok or 0),
                        failed=int(c.waves_failed or 0),
                        deferred=0,
                    )
                    c.updated_at = now
                    db.commit()
                    revoke_wave_authorization(db, int(campaign_id))
                    release_campaign_claim(db, int(campaign_id), worker_id=worker_id, force=True)
                    return {
                        "ok": True,
                        "skipped": False,
                        "error": None,
                        "campaign_id": int(campaign_id),
                        "campaign": campaign_to_dict(c),
                        "selection": selection,
                    }

                # Still unfinished: capacity-blocked today vs waiting for next day/slot
                if today_rem > 0 and cap_blocked > 0:
                    c.last_error = "blocked_capacity"
                    c.next_wave_at = now + timedelta(hours=1)
                    c.updated_at = now
                    log_autostory_event(
                        "autostory.campaign.paused",
                        level="warning",
                        campaign_id=int(campaign_id),
                        reason="blocked_capacity",
                    )
                    db.commit()
                    revoke_wave_authorization(db, int(campaign_id))
                    release_campaign_claim(db, int(campaign_id), worker_id=worker_id, force=True)
                    return {
                        "ok": False,
                        "skipped": True,
                        "error": "blocked_capacity",
                        "campaign_id": int(campaign_id),
                        "campaign": campaign_to_dict(c),
                        "selection": selection,
                    }

                # Done for today (or not runnable) but future work remains — next pickup
                nxt = next_wave_after(
                    now=now, times=list(c.times_json or []), ends_at=c.ends_at
                )
                if nxt is None:
                    c.status = "completed"
                    c.next_wave_at = None
                    c.last_error = None
                else:
                    c.next_wave_at = nxt
                    c.last_error = None
                    c.status = "active"
                c.updated_at = now
                db.commit()
                revoke_wave_authorization(db, int(campaign_id))
                release_campaign_claim(db, int(campaign_id), worker_id=worker_id, force=True)
                return {
                    "ok": True,
                    "skipped": True,
                    "error": None if nxt else None,
                    "campaign_id": int(campaign_id),
                    "campaign": campaign_to_dict(c),
                    "selection": selection,
                    "deferred_to_next_slot": nxt.isoformat() if nxt else None,
                }

            # Fail-closed certification at execution
            cert_denied: list[dict[str, Any]] = []
            certified_ids: list[int] = []
            for aid in account_ids:
                cok, creason = is_account_certified_publish(db, aid)
                if not cok:
                    cert_denied.append({"account_id": aid, "reason": creason})
                    ensure_progress_row(
                        db,
                        campaign_id=int(campaign_id),
                        wave_index=wave_index,
                        account_id=aid,
                        status="deferred",
                    )
                    update_progress(
                        db,
                        campaign_id=int(campaign_id),
                        wave_index=wave_index,
                        account_id=aid,
                        status="deferred",
                        error=creason,
                    )
                    log_autostory_event(
                        "autostory.account.deferred",
                        level="warning",
                        campaign_id=int(campaign_id),
                        wave_number=wave_index + 1,
                        account_id=aid,
                        result="deferred",
                        reason=creason,
                    )
                else:
                    certified_ids.append(aid)
            account_ids = certified_ids
            if not account_ids:
                revoke_wave_authorization(db, int(campaign_id))
                release_campaign_claim(db, int(campaign_id), worker_id=worker_id, force=True)
                return {
                    "ok": False,
                    "error": "no_certified_accounts_in_wave",
                    "cert_denied": cert_denied,
                    "campaign_id": int(campaign_id),
                }

            emit_autostory_system_log(
                db,
                "autostory.wave.started",
                campaign_id=int(campaign_id),
                wave_number=wave_index + 1,
                wave_size=len(account_ids),
                account_ids=list(account_ids),
            )
            db.commit()

            # Leave DB session before Telegram precheck refresh
            camp_id_for_auth = int(campaign_id)
            wave_index_for_auth = wave_index
            account_ids_for_auth = list(account_ids)
            media_path_snap = c.media_path
            caption_snap = c.caption or ""
            mps_snap = int(c.mentions_per_story or 0)
            msc_snap = c.mention_source_chat_id
            conf_snap = c.confirmation_token
            remaining_after = int(selection.get("remaining_after_wave") or 0)
            wave_truncated = bool(selection.get("wave_truncated"))

        from src.stories.autostory_media import validate_campaign_execution_policy, validate_campaign_media

        # Re-check current policy immediately before auth/mutation (policy may change after claim)
        policy_again = validate_campaign_execution_policy(
            {
                "status": "active",
                "media_path": media_path_snap,
                "mentions_per_story": mps_snap,
                "account_ids": account_ids_for_auth,
                "times_json": [],
            }
        )
        if not policy_again.get("ok"):
            now = datetime.utcnow()
            with get_db_context() as db:
                c = db.get(AutoStoryCampaign, int(campaign_id))
                if c is not None:
                    c.last_error = str(policy_again.get("reason") or "BLOCKED_POLICY")[:2000]
                    c.updated_at = now
                    if not operator_manual and require_scheduler_flag:
                        c.next_wave_at = now + timedelta(minutes=15)
                    db.commit()
                revoke_wave_authorization(db, int(campaign_id))
                release_campaign_claim(db, int(campaign_id), worker_id=worker_id, force=True)
            return {
                "ok": False,
                "error": "BLOCKED_POLICY",
                "reason": policy_again.get("reason"),
                "classification": policy_again.get("classification"),
                "message": policy_again.get("message"),
                "blockers": policy_again.get("blockers"),
                "campaign_id": int(campaign_id),
            }
        media_path_snap = str(
            (policy_again.get("media") or {}).get("path")
            or validate_campaign_media(media_path_snap).get("path")
            or media_path_snap
        )
        # Mentions: only reach here if policy allows current persisted value
        # (uncertified + mentions>0 already blocked above)

        from src.stories.autostory_hardening import ensure_fresh_story_auth_for_accounts

        fresh_gate = _run_coro_sync(ensure_fresh_story_auth_for_accounts(account_ids_for_auth))
        if fresh_gate.get("failed"):
            failed_ids = {int(x["account_id"]) for x in fresh_gate["failed"]}
            with get_db_context() as db:
                for item in fresh_gate["failed"]:
                    update_progress(
                        db,
                        campaign_id=camp_id_for_auth,
                        wave_index=wave_index_for_auth,
                        account_id=int(item["account_id"]),
                        status="deferred",
                        error=str(item.get("error") or "fresh_auth_failed"),
                    )
            account_ids_for_auth = [a for a in account_ids_for_auth if a not in failed_ids]
            if not account_ids_for_auth:
                now = datetime.utcnow()
                with get_db_context() as db:
                    c = db.get(AutoStoryCampaign, camp_id_for_auth)
                    if c is not None:
                        c.last_error = "fresh_story_auth_failed"[:2000]
                        c.updated_at = now
                        # Same backoff as the BLOCKED_POLICY path above. Without this the
                        # campaign's next_wave_at stays due, and the 45s scheduler loop
                        # (LOOP_INTERVAL_SEC) re-claims and re-attempts a fresh
                        # CanSendStoryRequest every tick -- confirmed in production to
                        # repeat ~100+ times over ~90 minutes for a single rate-limited
                        # account before ends_at forced the campaign to complete.
                        if not operator_manual and require_scheduler_flag:
                            c.next_wave_at = now + timedelta(minutes=FRESH_AUTH_FAILURE_BACKOFF_MINUTES)
                        db.commit()
                    retry_at_iso = (now + timedelta(minutes=FRESH_AUTH_FAILURE_BACKOFF_MINUTES)).isoformat()
                    # Durable (SystemLog row), not just structlog: precheck failure detail
                    # for a deferred wave was previously only visible via structlog, which
                    # this incident showed is not reliably captured in production (no
                    # journald/file record survived for the Slot 1 delay window).
                    #
                    # One row per actual state TRANSITION, not one per retry: a long
                    # cooldown (e.g. a multi-hour STORIES_TOO_MUCH block) would otherwise
                    # write a near-identical row every FRESH_AUTH_FAILURE_BACKOFF_MINUTES
                    # for as long as the account stays blocked. Only write when the
                    # error/blocked_until actually differs from the last recorded row for
                    # this (campaign, account) pair.
                    for item in fresh_gate["failed"]:
                        _record_account_deferred_if_new_state(
                            db,
                            campaign_id=camp_id_for_auth,
                            wave_index=wave_index_for_auth,
                            account_id=int(item["account_id"]),
                            error=str(item.get("error") or "")[:200],
                            reason=str(item.get("reason") or "")[:200],
                            blocked_until=item.get("blocked_until"),
                            retry_at=retry_at_iso,
                        )
                    db.commit()
                    revoke_wave_authorization(db, camp_id_for_auth)
                    release_campaign_claim(db, camp_id_for_auth, worker_id=worker_id, force=True)
                return {
                    "ok": False,
                    "error": "fresh_story_auth_failed",
                    "fresh_auth": fresh_gate,
                    "campaign_id": camp_id_for_auth,
                }

        with get_db_context() as db:
            c = db.get(AutoStoryCampaign, camp_id_for_auth)
            if c is None or c.status != "active":
                release_campaign_claim(db, camp_id_for_auth, worker_id=worker_id, force=True)
                return {"ok": False, "error": "campaign_not_active", "campaign_id": camp_id_for_auth}

            account_ids = list(account_ids_for_auth)
            authorize_wave(db, camp_id_for_auth, account_ids, wave_index=wave_index_for_auth)
            for aid in account_ids:
                ensure_progress_row(
                    db,
                    campaign_id=camp_id_for_auth,
                    wave_index=wave_index_for_auth,
                    account_id=aid,
                    status="claimed",
                )
                update_progress(
                    db,
                    campaign_id=camp_id_for_auth,
                    wave_index=wave_index_for_auth,
                    account_id=aid,
                    status="attempting",
                )
                from src.stories.autostory_operator_preview import log_autostory_event

                log_autostory_event(
                    "autostory.account.started",
                    campaign_id=camp_id_for_auth,
                    wave_number=wave_index_for_auth + 1,
                    account_id=aid,
                )

            durable_plan = load_durable_mention_plan(
                db,
                campaign_id=camp_id_for_auth,
                wave_index=wave_index_for_auth,
                account_ids=account_ids,
            )
            if durable_plan is not None:
                # A prior attempt at this exact (campaign, wave) slot already
                # selected targets -- reuse them so a retry never re-randomizes.
                mention_plan = [m for aid in account_ids for m in durable_plan[aid]]
            else:
                dry_payload = {
                    "account_ids": account_ids,
                    "media_path": media_path_snap,
                    "caption": caption_snap,
                    "mentions_per_story": mps_snap,
                    "mention_source_chat_id": msc_snap,
                    "mention_strategy": "random",
                    "max_stories": len(account_ids),
                    "dry_run": True,
                }
                plan = build_story_dry_run_plan(db, dry_payload)
                mention_plan = list(plan.get("selected_mention_candidates") or [])
                per_account_chunks = allocate_mentions_without_replacement(
                    mention_plan,
                    account_ids=account_ids,
                    mentions_per_story=mps_snap,
                )
                persist_mention_plan_for_wave(
                    db,
                    campaign_id=camp_id_for_auth,
                    wave_index=wave_index_for_auth,
                    per_account_plan=dict(zip(account_ids, per_account_chunks)),
                )
            live_payload = _wave_payload_from_campaign(
                c, mention_plan=mention_plan, account_ids=account_ids
            )
            # Keep confirmation token from snapshot if ORM detached oddly
            live_payload["confirmation_token"] = conf_snap
            report = build_story_rotation_precheck(db, live_payload)
            camp_snapshot = campaign_to_dict(c)
            wave_index = wave_index_for_auth

        err_body, status = evaluate_controlled_live_run_gates(live_payload, report)
        if err_body is not None:
            now = datetime.utcnow()
            with get_db_context() as db:
                c = db.get(AutoStoryCampaign, int(campaign_id))
                if c is not None:
                    c.last_error = str(err_body.get("error") or "gate_blocked")[:2000]
                    c.updated_at = now
                    if not operator_manual and require_scheduler_flag:
                        c.next_wave_at = now + timedelta(minutes=15)
                    db.commit()
                revoke_wave_authorization(db, int(campaign_id))
                release_campaign_claim(db, int(campaign_id), worker_id=worker_id, force=True)
            return {
                "ok": False,
                "error": err_body.get("error"),
                "message": err_body.get("message"),
                "live_gate_blockers": err_body.get("live_gate_blockers"),
                "campaign_id": int(campaign_id),
                "campaign": camp_snapshot,
                "http_status": status,
            }

        try:
            result = _run_coro_sync(_execute_controlled_live_story_run(live_payload, report))
        except Exception as exc:
            logger.exception("auto_story_wave_exception", campaign_id=campaign_id, error=str(exc))
            now = datetime.utcnow()
            with get_db_context() as db:
                c = db.get(AutoStoryCampaign, int(campaign_id))
                if c is not None:
                    _advance_after_wave(c, now=now, ok=False, run_id=None, error=str(exc))
                    db.commit()
                    camp_out = campaign_to_dict(c)
                else:
                    camp_out = camp_snapshot
                revoke_wave_authorization(db, int(campaign_id))
                release_campaign_claim(db, int(campaign_id), worker_id=worker_id, force=True)
            return {
                "ok": False,
                "error": "auto_story_wave_failed",
                "message": str(exc),
                "campaign_id": int(campaign_id),
                "campaign": camp_out,
            }

        now = datetime.utcnow()
        wave_ok = bool(result.get("ok"))
        ambiguous = result.get("result_classification") == "AMBIGUOUS_NO_RETRY"
        from src.stories.autostory_operator_preview import emit_autostory_system_log, log_autostory_event

        with get_db_context() as db:
            _record_wave_progress_from_result(
                db, campaign_id=int(campaign_id), wave_index=wave_index, result=result
            )
            # Recompute remaining unfinished after this wave
            c = db.get(AutoStoryCampaign, int(campaign_id))
            rem = 0
            if c is not None:
                sel2 = select_next_wave_accounts(db, c, wave_index=wave_index + 1)
                rem = int(sel2.get("unfinished_count") or 0)
                eligible_next = list(sel2.get("wave_account_ids") or [])
                from src.stories.autostory_recurring import is_recurring, recurring_campaign_complete

                if ambiguous:
                    c.last_error = "AMBIGUOUS_NO_RETRY"
                    # Pause progression — do not auto-advance
                    c.next_wave_at = None
                    c.updated_at = now
                    log_autostory_event(
                        "autostory.campaign.paused",
                        level="error",
                        campaign_id=int(campaign_id),
                        reason="AMBIGUOUS_NO_RETRY",
                    )
                elif wave_ok and (
                    rem <= 0
                    or (is_recurring(c) and recurring_campaign_complete(db, c))
                ):
                    c.waves_ok = int(c.waves_ok or 0) + 1
                    c.last_error = None
                    c.last_wave_at = now
                    if result.get("run_id") is not None:
                        c.last_story_run_id = int(result.get("run_id"))
                    c.status = "completed"
                    c.next_wave_at = None
                    c.updated_at = now
                else:
                    from src.stories.autostory_recurring import is_recurring as _is_recurring

                    continue_now = bool(wave_ok and eligible_next)
                    if _is_recurring(c):
                        # Same-day extra Stories wait for the next pickup slot.
                        # Immediate continuation is only for wave-size overflow
                        # of the current daily slot (fleet > MAX_AUTOSTORY_WAVE_SIZE).
                        continue_now = bool(wave_ok and wave_truncated)
                    _advance_after_wave(
                        c,
                        now=now,
                        ok=wave_ok,
                        run_id=result.get("run_id"),
                        error=None if wave_ok else str(result.get("error") or "wave_failed"),
                        remaining_accounts=rem if wave_ok else rem,
                        continue_immediately=continue_now,
                    )
                db.commit()
                camp_out = campaign_to_dict(c)
                camp_out["progress"] = _campaign_progress(
                    db, c, camp_out.get("max_story_publishes") or 0
                )
            else:
                camp_out = camp_snapshot
            emit_autostory_system_log(
                db,
                "autostory.wave.completed",
                campaign_id=int(campaign_id),
                wave_number=wave_index + 1,
                wave_size=len(account_ids),
                story_run_id=result.get("run_id"),
                successful=int(result.get("stories_ok") or 0),
                failed=int(result.get("stories_failed") or 0),
            )
            if camp_out.get("status") == "completed":
                emit_autostory_system_log(
                    db,
                    "autostory.campaign.completed",
                    campaign_id=int(campaign_id),
                    story_run_id=result.get("run_id"),
                    successful=int(result.get("stories_ok") or 0),
                    failed=int(result.get("stories_failed") or 0),
                    deferred=0,
                )
            db.commit()
            revoke_wave_authorization(db, int(campaign_id))
            release_campaign_claim(db, int(campaign_id), worker_id=worker_id, force=True)

        return {
            "ok": wave_ok and not ambiguous,
            "campaign_id": int(campaign_id),
            "campaign": camp_out,
            "wave": result,
            "wave_index": wave_index,
            "wave_account_ids": account_ids,
            "max_autostory_wave_size": MAX_AUTOSTORY_WAVE_SIZE,
            "operator_manual": operator_manual,
            "per_account_mentions": plan.get("per_account_mentions") or [],
            "authorization_revoked": True,
        }
    except Exception as exc:
        logger.exception("auto_story_execute_wave_unhandled", campaign_id=campaign_id, error=str(exc))
        try:
            with get_db_context() as db:
                revoke_wave_authorization(db, int(campaign_id))
                release_campaign_claim(db, int(campaign_id), worker_id=worker_id, force=True)
        except Exception:
            pass
        return {
            "ok": False,
            "error": "auto_story_wave_unhandled",
            "message": str(exc),
            "campaign_id": int(campaign_id),
        }


def tick_due_auto_story_campaigns(*, now: datetime | None = None, limit: int = 3) -> dict[str, Any]:
    """Scheduler entry: claim due campaigns then execute one bounded wave each."""
    from src.stories.autostory_hardening import claim_campaign, worker_identity
    from src.stories.mutation_boundary import story_mutations_enabled

    if not scheduler_story_execution_enabled():
        return {"skipped": True, "reason": "scheduler_story_execution_disabled", "fired": []}
    if not story_mutations_enabled():
        return {"skipped": True, "reason": "story_mutations_disabled", "fired": []}

    now = now or datetime.utcnow()
    worker_id = worker_identity()
    fired: list[dict[str, Any]] = []
    due_ids: list[int] = []

    with get_db_context() as db:
        due = (
            db.query(AutoStoryCampaign)
            .filter(
                AutoStoryCampaign.status == "active",
                AutoStoryCampaign.next_wave_at.isnot(None),
                AutoStoryCampaign.next_wave_at <= now,
            )
            .order_by(AutoStoryCampaign.next_wave_at.asc())
            .limit(int(limit))
            .all()
        )
        candidates = [int(c.id) for c in due]
        expired = (
            db.query(AutoStoryCampaign)
            .filter(
                AutoStoryCampaign.status == "active",
                AutoStoryCampaign.ends_at.isnot(None),
                AutoStoryCampaign.ends_at <= now,
            )
            .all()
        )
        for c in expired:
            if c.next_wave_at is None or c.next_wave_at > c.ends_at:
                c.status = "completed"
                c.next_wave_at = None
                c.updated_at = now
        db.commit()

    for cid in candidates:
        won, meta = False, {}
        with get_db_context() as db:
            won, meta = claim_campaign(db, cid, worker_id=worker_id, now=now)
        if not won:
            fired.append(
                {
                    "ok": False,
                    "skipped": True,
                    "error": "campaign_claim_lost",
                    "campaign_id": cid,
                    "claim": meta,
                }
            )
            continue
        due_ids.append(cid)
        result = execute_wave(
            cid,
            operator_manual=False,
            require_scheduler_flag=True,
            worker_id=worker_id,
            already_claimed=True,
        )
        fired.append(result)

    if not due_ids:
        logger.debug("autostory.scheduler.idle", reason="no_campaigns_due")

    return {"skipped": False, "reason": "ok", "fired": fired, "due_ids": due_ids}
