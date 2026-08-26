"""Canonical AutoStory operator preview + fleet summary (single scheduling truth)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

import structlog

from src.stories.auto_story_service import (
    AWAKE_END_MINUTES,
    AWAKE_START_MINUTES,
    compute_daily_times,
    next_wave_after,
)
from src.stories.autostory_hardening import (
    MAX_AUTOSTORY_WAVE_SIZE,
    account_has_daily_capacity,
    is_account_certified_publish,
    plan_full_fleet_waves,
    rotate_account_ids,
)

logger = structlog.get_logger(__name__)


def operator_timezone_name() -> str:
    try:
        from config.settings import settings

        return str(getattr(settings, "sched_default_timezone", None) or "UTC").strip() or "UTC"
    except Exception:
        return "UTC"


def _parse_hhmm(value: str) -> tuple[int, int] | None:
    parts = str(value or "").strip().split(":")
    if len(parts) < 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def local_awake_hhmm_to_utc_hhmm(local_hhmm: str, *, tz_name: str | None = None) -> str:
    """Convert one operator-local HH:MM awake slot to UTC HH:MM for the engine.

    Engine ``times_json`` / ``next_wave_after`` use naive UTC wall-clock HH:MM.
    Awake windows are defined in the operator timezone (e.g. 10:00–20:00 Asia/Yerevan).
    """
    tz_name = tz_name or operator_timezone_name()
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    parsed = _parse_hhmm(local_hhmm)
    if parsed is None:
        return str(local_hhmm)
    hh, mm = parsed
    # Anchor on "today" in operator TZ; DST-stable for Asia/Yerevan.
    local_now = datetime.now(tz)
    local_dt = local_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    utc_dt = local_dt.astimezone(timezone.utc)
    return f"{utc_dt.hour:02d}:{utc_dt.minute:02d}"


def awake_window_times_utc(*, posts_per_day: int, tz_name: str | None = None) -> dict[str, Any]:
    """Generate awake-window pickup HH:MMs as UTC for storage, with local labels."""
    tz_name = tz_name or operator_timezone_name()
    ppd = max(1, min(12, int(posts_per_day or 1)))
    local_times = compute_daily_times(ppd)
    utc_times: list[str] = []
    seen: set[str] = set()
    for lt in local_times:
        ut = local_awake_hhmm_to_utc_hhmm(lt, tz_name=tz_name)
        if ut not in seen:
            seen.add(ut)
            utc_times.append(ut)
    window = (
        f"{AWAKE_START_MINUTES // 60:02d}:{AWAKE_START_MINUTES % 60:02d}"
        f"-{AWAKE_END_MINUTES // 60:02d}:{AWAKE_END_MINUTES % 60:02d}"
    )
    return {
        "schedule_mode": "awake_window",
        "times_json": utc_times,
        "local_times_json": local_times,
        "explicit": False,
        "awake_window": window,
        "awake_window_timezone": tz_name,
        "timezone": tz_name,
    }


def normalize_times_json(raw: Any, *, posts_per_day: int | None = None) -> dict[str, Any]:
    """Normalize operator times into execution ``times_json`` (UTC HH:MM list).

    Accepts HH:MM strings and/or ISO-8601 datetimes. Explicit input never falls
    back to the awake window.

    Awake-window mode generates local 10:00–20:00 slots in the operator timezone,
    then stores the matching UTC HH:MM values for ``next_wave_after``.
    """
    if raw is None or raw == "" or raw == []:
        return awake_window_times_utc(posts_per_day=int(posts_per_day or 1))

    items = raw if isinstance(raw, list) else [raw]
    times: list[str] = []
    display_absolute: list[dict[str, Any]] = []
    for item in items:
        s = str(item or "").strip()
        if not s:
            continue
        # ISO datetime → extract UTC HH:MM for engine + keep absolute display
        if "T" in s or s.endswith("Z") or "+" in s[10:]:
            try:
                iso = s.replace("Z", "+00:00")
                dt = datetime.fromisoformat(iso)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                dt_utc = dt.astimezone(timezone.utc)
                hhmm = f"{dt_utc.hour:02d}:{dt_utc.minute:02d}"
                times.append(hhmm)
                display_absolute.append(
                    {
                        "input": s,
                        "utc_iso": dt_utc.replace(tzinfo=None).isoformat() + "Z",
                        "hhmm_utc": hhmm,
                    }
                )
                continue
            except ValueError:
                pass
        parsed = _parse_hhmm(s)
        if parsed is None:
            continue
        hh, mm = parsed
        hhmm = f"{hh:02d}:{mm:02d}"
        times.append(hhmm)

    # Dedupe preserve order
    out: list[str] = []
    seen: set[str] = set()
    for t in times:
        if t not in seen:
            seen.add(t)
            out.append(t)

    if not out:
        generated = awake_window_times_utc(posts_per_day=int(posts_per_day or 1))
        generated["warning"] = "invalid_times_json_fell_back_to_awake_window"
        return generated

    return {
        "schedule_mode": "explicit_times",
        "times_json": out,
        "explicit": True,
        "awake_window": None,
        "absolute_inputs": display_absolute,
    }


def format_execution_times(
    times_json: list[str],
    *,
    start_at: datetime | None = None,
    ends_at: datetime | None = None,
    duration_days: int = 1,
    tz_name: str | None = None,
) -> list[dict[str, Any]]:
    """Human-readable upcoming execution/pickup slots from HH:MM times_json.

    Collects up to ``duration_days * len(times)`` slots within ``ends_at``.
    """
    tz_name = tz_name or operator_timezone_name()
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
        tz_name = "UTC"

    now = start_at or datetime.utcnow()
    # Cover full campaign length: start-of-day span so multi-day × posts/day is complete
    duration = max(1, int(duration_days or 1))
    ends = ends_at or (now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=duration + 1))
    rows: list[dict[str, Any]] = []
    cursor = now - timedelta(seconds=1)
    limit = max(1, duration * max(1, len(times_json or [])))
    for _ in range(limit + 8):
        nxt = next_wave_after(now=cursor, times=list(times_json or []), ends_at=ends)
        if nxt is None:
            break
        utc_aware = nxt.replace(tzinfo=timezone.utc)
        local = utc_aware.astimezone(tz)
        rows.append(
            {
                "utc_iso": nxt.isoformat() + "Z",
                "utc_label": nxt.strftime("%b %d, %Y · %H:%M UTC"),
                "local_iso": local.replace(tzinfo=None).isoformat(),
                "local_label": local.strftime("%b %d, %Y · %H:%M") + f" {tz_name}",
                "local_day": local.strftime("%b %d, %Y"),
                "local_time": local.strftime("%H:%M"),
                "hhmm_utc": nxt.strftime("%H:%M"),
            }
        )
        cursor = nxt
        if len(rows) >= limit:
            break
    return rows


def group_executions_by_day(executions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for row in executions:
        day = row.get("local_day") or row.get("utc_label", "")[:12]
        if day not in groups:
            groups[day] = []
            order.append(day)
        groups[day].append(row)
    out = []
    for d in order:
        slots = sorted(groups[d], key=lambda s: str(s.get("local_time") or s.get("hhmm_utc") or ""))
        out.append({"day": d, "slots": slots, "count": len(slots)})
    return out


def local_pickups_within_awake_window(executions: list[dict[str, Any]]) -> dict[str, Any]:
    """Verify displayed local pickup times fall inside 10:00–20:00 inclusive."""
    bad: list[dict[str, Any]] = []
    for row in executions:
        lt = str(row.get("local_time") or "")
        parsed = _parse_hhmm(lt)
        if parsed is None:
            continue
        mins = parsed[0] * 60 + parsed[1]
        if mins < AWAKE_START_MINUTES or mins > AWAKE_END_MINUTES:
            bad.append(row)
    return {"ok": not bad, "violations": bad, "window": f"{AWAKE_START_MINUTES // 60:02d}:{AWAKE_START_MINUTES % 60:02d}-{AWAKE_END_MINUTES // 60:02d}:{AWAKE_END_MINUTES % 60:02d}"}



def fleet_autostory_summary(db) -> dict[str, Any]:
    """Dynamic Production Ready / Available Today counters for operator UI."""
    from src.core.account_protection import PROTECTED_IDS, PURPOSE_HOLD_IDS
    from src.ai_agent.account_allowlist import RESERVED_AI_AGENT_ACCOUNT_IDS
    from src.core.models import Account
    from src.stories.fleet_certification import durable_certification_evidence
    from src.stories.story_auth_state import story_auth_is_fresh
    from sqlalchemy import text

    total = int(db.query(Account).count())
    evidence = durable_certification_evidence(db)
    certified_ids = sorted(evidence.keys())
    production_ready = len(certified_ids)

    eligible: list[int] = []
    at_capacity: list[int] = []
    needs_fresh_auth: list[int] = []
    unavailable: list[int] = []

    for aid in certified_ids:
        if aid in PROTECTED_IDS or aid in PURPOSE_HOLD_IDS or aid in RESERVED_AI_AGENT_ACCOUNT_IDS:
            unavailable.append(aid)
            continue
        cok, _ = is_account_certified_publish(db, aid)
        if not cok:
            unavailable.append(aid)
            continue
        lock = db.execute(
            text("SELECT 1 FROM auto_story_account_locks WHERE account_id = :aid"),
            {"aid": int(aid)},
        ).fetchone()
        if lock:
            unavailable.append(aid)
            continue
        cap_ok, _ = account_has_daily_capacity(db, aid)
        if not cap_ok:
            at_capacity.append(aid)
            continue
        acc = db.get(Account, aid)
        if acc is None:
            unavailable.append(aid)
            continue
        if not story_auth_is_fresh(acc):
            needs_fresh_auth.append(aid)
        # Stale cached auth does not remove Available Today — runtime refreshes before publish.
        eligible.append(aid)

    available_today = len(eligible)
    return {
        "total_accounts": total,
        "production_ready": production_ready,
        "production_ready_label": "CERTIFIED_PUBLISH",
        "available_today": available_today,
        "eligible_now": available_today,  # alias for older clients
        "eligible_account_ids": eligible,
        "eligible_account_ids_truncated": False,
        "at_daily_limit": len(at_capacity),
        "at_daily_capacity": len(at_capacity),  # alias
        "temporarily_blocked": len(unavailable),
        "unavailable": len(unavailable),
        "needs_fresh_auth": len(needs_fresh_auth),  # diagnostics only
        "diagnostics": {
            "auth_refresh_before_publish": len(needs_fresh_auth),
            "note": "Authorization is checked automatically before publishing.",
        },
        "policy_excluded": len(
            set(PROTECTED_IDS) | set(PURPOSE_HOLD_IDS) | set(RESERVED_AI_AGENT_ACCOUNT_IDS)
        ),
        "max_autostory_wave_size": MAX_AUTOSTORY_WAVE_SIZE,
        "timezone": operator_timezone_name(),
        "help": (
            "Production Ready means the account has passed live Story certification. "
            "Available Today means currently selectable based on policy and daily capacity. "
            "Authorization is refreshed automatically before publishing."
        ),
        "_eligible_all_ids": eligible,
    }


def plan_waves_for_accounts(db, account_ids: list[int]) -> dict[str, Any]:
    plan = plan_full_fleet_waves(db, [int(x) for x in account_ids])
    return {
        "account_count": plan["account_count"],
        "wave_count": plan["wave_count"],
        "waves": plan["waves"],
        "max_wave_size": plan["max_wave_size"],
        "duplicates": plan["duplicates"],
        "any_over_max": plan["any_over_max"],
    }


def select_automatic_accounts(
    db,
    *,
    requested: int | str,
    exclude_ids: list[int] | None = None,
) -> dict[str, Any]:
    fleet = fleet_autostory_summary(db)
    eligible = list(fleet.get("_eligible_all_ids") or fleet.get("eligible_account_ids") or [])
    if exclude_ids:
        ex = {int(x) for x in exclude_ids}
        eligible = [a for a in eligible if a not in ex]
    rotated = rotate_account_ids(db, eligible)

    if str(requested).lower() in {"all", "all_eligible", "*"}:
        want = len(rotated)
        requested_label = "all_eligible"
    else:
        want = max(0, int(requested))
        requested_label = str(want)

    selected = rotated[:want] if want else []
    shortfall = max(0, want - len(selected)) if requested_label != "all_eligible" else 0
    return {
        "selection_mode": "automatic",
        "requested": requested_label,
        "requested_count": want,
        "selected_account_ids": selected,
        "selected_count": len(selected),
        "eligible_available": len(rotated),
        "shortfall": shortfall,
        "production_ready": fleet["production_ready"],
        "acknowledgment_required": shortfall > 0,
        "message": (
            f"Only {len(rotated)} accounts are currently eligible. Requested: {want}."
            if shortfall > 0
            else None
        ),
    }


def build_operator_campaign_preview(db, payload: dict[str, Any]) -> dict[str, Any]:
    """Single scheduling + wave truth used by UI preview and create response.

    Modes:
    - accounts_publish_once (legacy): max Stories = planned account count.
    - recurring_daily: max = accounts × stories_per_account_per_day × duration_days.
    """
    from src.stories.autostory_recurring import (
        CAMPAIGN_MODE_ONCE,
        CAMPAIGN_MODE_RECURRING,
        awake_bounds,
        clamp_stories_per_account_per_day,
        max_story_publishes,
        plan_recurring_times_json,
    )

    duration_days = max(1, min(90, int(payload.get("duration_days") or 1)))
    raw_mode = str(payload.get("campaign_mode") or CAMPAIGN_MODE_RECURRING).strip().lower()
    if raw_mode not in {CAMPAIGN_MODE_ONCE, CAMPAIGN_MODE_RECURRING}:
        raw_mode = CAMPAIGN_MODE_RECURRING
    # Explicit legacy once-mode if caller forces it
    if payload.get("legacy_once") is True:
        raw_mode = CAMPAIGN_MODE_ONCE

    spad = clamp_stories_per_account_per_day(payload.get("stories_per_account_per_day", 1))
    start_hhmm, end_hhmm = awake_bounds(
        {
            "awake_start_hhmm": payload.get("awake_start_hhmm") or payload.get("window_start"),
            "awake_end_hhmm": payload.get("awake_end_hhmm") or payload.get("window_end"),
        }
    )

    fleet = fleet_autostory_summary(db)
    selection_mode = str(payload.get("selection_mode") or "manual").strip().lower()
    account_ids = [int(x) for x in (payload.get("account_ids") or [])]
    allow_fewer = bool(
        payload.get("acknowledge_eligibility_shortfall")
        or payload.get("allow_fewer")
    )

    auto_meta = None
    requested_count = len(account_ids)
    approval_blocked = False
    approval_block_reason = None

    if selection_mode == "automatic" or payload.get("automatic_count") is not None:
        auto_meta = select_automatic_accounts(
            db,
            requested=payload.get("automatic_count")
            if payload.get("automatic_count") is not None
            else payload.get("requested_count") or len(account_ids) or 0,
        )
        requested_count = int(auto_meta["requested_count"])
        if auto_meta.get("shortfall", 0) > 0 and not allow_fewer:
            approval_blocked = True
            approval_block_reason = auto_meta.get("message") or (
                f"Requested {requested_count}; only {auto_meta['eligible_available']} available today."
            )
            account_ids = []
        else:
            account_ids = list(auto_meta["selected_account_ids"])
        selection_mode = "automatic"
    else:
        requested_count = len(account_ids)

    planned_count = len(account_ids)

    # Schedule: recurring auto-generates pickups; legacy uses posts_per_day / times_json
    if raw_mode == CAMPAIGN_MODE_RECURRING and not payload.get("times_json"):
        sched = plan_recurring_times_json(
            account_count=max(1, planned_count or requested_count or 1),
            stories_per_account_per_day=spad,
            awake_start=start_hhmm,
            awake_end=end_hhmm,
            tz_name=operator_timezone_name(),
        )
        posts_per_day = int(sched.get("pickup_opportunities") or 1)
    else:
        posts_per_day = max(1, min(12, int(payload.get("posts_per_day") or 1)))
        sched = normalize_times_json(
            payload.get("times_json"),
            posts_per_day=posts_per_day,
        )
        # Apply custom window for awake generation when times empty
        if not sched.get("explicit") and not payload.get("times_json"):
            from src.stories.autostory_recurring import spread_local_times, local_times_to_utc_hhmm

            local_t = spread_local_times(
                start_hhmm=start_hhmm, end_hhmm=end_hhmm, count=posts_per_day
            )
            sched = {
                "schedule_mode": "awake_window",
                "times_json": local_times_to_utc_hhmm(local_t),
                "local_times_json": local_t,
                "explicit": False,
                "awake_window": f"{start_hhmm}-{end_hhmm}",
            }
        if sched["explicit"]:
            posts_per_day = max(1, len(sched["times_json"]))

    times = list(sched["times_json"])
    now = datetime.utcnow()
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    ends = day0 + timedelta(days=duration_days + 1)
    first = next_wave_after(now=now - timedelta(seconds=1), times=times, ends_at=ends)
    executions = format_execution_times(
        times, start_at=now, ends_at=ends, duration_days=duration_days
    )
    by_day = group_executions_by_day(executions)

    wave_plan = plan_waves_for_accounts(db, account_ids) if account_ids else {
        "account_count": 0,
        "wave_count": 0,
        "waves": [],
        "max_wave_size": MAX_AUTOSTORY_WAVE_SIZE,
        "duplicates": 0,
        "any_over_max": False,
    }

    caption = payload.get("caption") or ""
    media_path = str(payload.get("media_path") or "").strip()
    from src.stories.autostory_media import normalize_campaign_mentions, validate_campaign_media

    mention_norm = normalize_campaign_mentions(payload.get("mentions_per_story", 0))
    mentions = int(mention_norm["mentions_per_story"])

    media_check = validate_campaign_media(media_path) if media_path else {
        "ok": False,
        "media_ok": False,
        "path": None,
        "message": "Add Story media to continue.",
        "error": "media_path_required",
        "compat_blocker": "media_path_required",
    }
    media_ok = bool(media_check.get("ok"))
    if media_path and media_check.get("path"):
        media_path = str(media_check["path"])

    if not media_ok:
        approval_blocked = True
        approval_block_reason = approval_block_reason or (
            media_check.get("message") or "Media is missing or not Story-compatible."
        )

    if mention_norm.get("mentions_forced_off"):
        approval_blocked = True
        approval_block_reason = approval_block_reason or mention_norm.get("message")

    window_check = None
    if not sched["explicit"]:
        # Validate against configured awake window (not hardcoded 10-20 only)
        window_check = _local_pickups_within_window(
            executions, start_hhmm=start_hhmm, end_hhmm=end_hhmm
        )
        if not window_check.get("ok"):
            approval_blocked = True
            approval_block_reason = approval_block_reason or (
                "Generated pickup times fall outside the configured awake window."
            )

    # First-day helper
    schedule_starts = "includes_today"
    if executions:
        try:
            tz = ZoneInfo(operator_timezone_name())
            today_local = datetime.now(tz).strftime("%b %d, %Y")
            first_day = executions[0].get("local_day")
            if first_day and first_day != today_local:
                schedule_starts = "starts_tomorrow"
        except Exception:
            pass

    if raw_mode == CAMPAIGN_MODE_RECURRING:
        max_story_publishes_n = max_story_publishes(planned_count, spad, duration_days)
        semantics = {
            "model": CAMPAIGN_MODE_RECURRING,
            "explanation": (
                f"Each selected account may publish up to {spad} Story/Stories per day "
                f"for {duration_days} day(s). Maximum Stories = accounts × "
                f"stories/account/day × days. Waves ≤{MAX_AUTOSTORY_WAVE_SIZE}."
            ),
            "accounts_per_execution_wave": min(planned_count, MAX_AUTOSTORY_WAVE_SIZE) if planned_count else 0,
            "schedule_pickup_slots": len(executions),
            "max_story_publishes": max_story_publishes_n,
            "stories_per_account_per_day": spad,
        }
    else:
        max_story_publishes_n = planned_count
        semantics = {
            "model": CAMPAIGN_MODE_ONCE,
            "explanation": (
                "Selected accounts each publish at most one Story for this campaign. "
                "Schedule times are pickup windows for remaining unfinished accounts "
                f"(waves ≤{MAX_AUTOSTORY_WAVE_SIZE}). "
                "They are not republish multipliers."
            ),
            "accounts_per_execution_wave": min(planned_count, MAX_AUTOSTORY_WAVE_SIZE) if planned_count else 0,
            "schedule_pickup_slots": len(executions),
            "max_story_publishes": max_story_publishes_n,
        }

    execution_wave_groups = []
    for i, slot in enumerate(executions):
        execution_wave_groups.append(
            {
                "execution_index": i + 1,
                "when": slot.get("local_label") or slot.get("utc_label"),
                "role": "pickup_window",
                "wave_plan": wave_plan if i == 0 else {
                    "note": "Later slots only run if unfinished daily targets remain.",
                    "waves": wave_plan.get("waves") or [],
                },
            }
        )

    return {
        "ok": True,
        "campaign_mode": raw_mode,
        "stories_per_account_per_day": spad,
        "awake_start_hhmm": start_hhmm,
        "awake_end_hhmm": end_hhmm,
        "schedule_mode": sched["schedule_mode"],
        "explicit_times": bool(sched["explicit"]),
        "times_json": times,
        "duration_days": duration_days,
        "posts_per_day": posts_per_day,
        "awake_window": sched.get("awake_window") if not sched["explicit"] else None,
        "timezone": operator_timezone_name(),
        "ends_at": ends.isoformat() + "Z",
        "first_wave_at": (first.isoformat() + "Z") if first else None,
        "execution_times": executions,
        "schedule_by_day": by_day,
        "planned_execution_count": len(executions),
        "fleet": {
            "production_ready": fleet["production_ready"],
            "available_today": fleet["available_today"],
            "eligible_now": fleet["available_today"],
            "at_daily_limit": fleet["at_daily_limit"],
            "temporarily_blocked": fleet["temporarily_blocked"],
            "diagnostics": fleet["diagnostics"],
            "help": fleet["help"],
        },
        "selection_mode": selection_mode,
        "requested_count": requested_count,
        "planned_count": planned_count,
        "allow_fewer": allow_fewer,
        "account_ids": account_ids,
        "account_count": planned_count,
        "automatic": auto_meta,
        "wave_plan": wave_plan,
        "execution_wave_groups": execution_wave_groups,
        "max_autostory_wave_size": MAX_AUTOSTORY_WAVE_SIZE,
        "media_path": media_path or None,
        "media_ok": media_ok,
        "media_validation": {
            "ok": media_ok,
            "message": media_check.get("message"),
            "compat_blocker": media_check.get("compat_blocker"),
            "filename": media_check.get("filename"),
            "absolute_path": media_check.get("absolute_path"),
            "needs_preparation": bool(media_check.get("needs_preparation")),
        },
        "caption": caption,
        "mentions_per_story": mentions,
        "mentions_enabled": bool(mention_norm.get("mentions_enabled")),
        "mentions_off": mentions == 0,
        "mentions_production_certified": bool(mention_norm.get("mentions_production_certified")),
        "mentions_message": mention_norm.get("message"),
        "stories_per_account": spad if raw_mode == CAMPAIGN_MODE_RECURRING else 1,
        "max_story_publishes": max_story_publishes_n,
        "campaign_semantics": semantics,
        "schedule_starts": schedule_starts,
        "schedule_starts_label": (
            "Includes today" if schedule_starts == "includes_today" else "Starts tomorrow"
        ),
        "awake_window_local_ok": None if window_check is None else bool(window_check.get("ok")),
        "approval_blocked": approval_blocked,
        "approval_block_reason": approval_block_reason,
        "can_approve": (not approval_blocked) and media_ok and planned_count > 0,
        "safety": {
            "fresh_auth_before_publish": True,
            "automatic_reconciliation": True,
            "automatic_relock": True,
            "no_systemd_changes_required": True,
            "only_approved_waves_authorized": True,
        },
        "summary_lines": _summary_lines(
            fleet=fleet,
            requested_count=requested_count,
            planned_count=planned_count,
            allow_fewer=allow_fewer,
            executions=executions,
            by_day=by_day,
            wave_plan=wave_plan,
            selection_mode=selection_mode,
            schedule_mode=sched["schedule_mode"],
            awake_window=sched.get("awake_window") if not sched["explicit"] else None,
            duration_days=duration_days,
            posts_per_day=posts_per_day,
            mentions=mentions,
            max_story_publishes=max_story_publishes_n,
            timezone=operator_timezone_name(),
        ),
    }


def _local_pickups_within_window(
    executions: list[dict[str, Any]],
    *,
    start_hhmm: str,
    end_hhmm: str,
) -> dict[str, Any]:
    from src.stories.autostory_recurring import parse_hhmm

    sh, sm = parse_hhmm(start_hhmm, "10:00")
    eh, em = parse_hhmm(end_hhmm, "20:00")
    start_m = sh * 60 + sm
    end_m = eh * 60 + em
    bad: list[dict[str, Any]] = []
    for row in executions:
        lt = str(row.get("local_time") or "")
        parsed = _parse_hhmm(lt)
        if parsed is None:
            continue
        mins = parsed[0] * 60 + parsed[1]
        if mins < start_m or mins > end_m:
            bad.append(row)
    return {
        "ok": not bad,
        "violations": bad,
        "window": f"{start_hhmm}-{end_hhmm}",
    }


def _summary_lines(
    *,
    fleet: dict[str, Any],
    requested_count: int,
    planned_count: int,
    allow_fewer: bool,
    executions: list[dict[str, Any]],
    by_day: list[dict[str, Any]],
    wave_plan: dict[str, Any],
    selection_mode: str,
    schedule_mode: str,
    awake_window: str | None,
    duration_days: int,
    posts_per_day: int,
    mentions: int,
    max_story_publishes: int,
    timezone: str,
) -> list[str]:
    lines = [
        f"Production Ready: {fleet['production_ready']}",
        f"Available Today: {fleet['available_today']}",
    ]
    if requested_count != planned_count:
        lines.append(
            f"Requested {requested_count} · Planned now {planned_count}"
            + (" (fewer allowed)" if allow_fewer else "")
        )
    else:
        lines.append(f"Accounts: {planned_count}")
    lines.append(f"Maximum Story publishes: {max_story_publishes} (each account at most once)")
    if schedule_mode == "awake_window":
        lines.append(
            f"Schedule: {duration_days} day(s) · {posts_per_day}/day · window {awake_window} · {timezone}"
        )
        lines.append(f"Pickup slots: {len(executions)} (remaining accounts only)")
    else:
        lines.append(f"Exact schedule · {len(executions)} time(s) · {timezone}")
    for day in by_day:
        times = ", ".join(s.get("local_time") or s.get("hhmm_utc") for s in day["slots"])
        lines.append(f"  {day['day']}: {times}")
    waves = wave_plan.get("waves") or []
    if waves:
        parts = [f"{w['size']}" for w in waves]
        lines.append(f"First-fire waves (≤{MAX_AUTOSTORY_WAVE_SIZE}): " + " + ".join(parts))
    lines.append(
        "Selection: "
        + ("Automatic · least recently used" if selection_mode == "automatic" else "Manual")
    )
    lines.append("Mentions: Off" if mentions == 0 else f"Mentions: {mentions}")
    lines.append("Authorization checked automatically before publishing")
    lines.append("Automatic reconciliation + automatic cleanup")
    return lines


_SECRET_KEYS = frozenset({
    "session_string", "session", "token", "password", "otp", "api_hash", "api_id",
    "phone", "phone_number", "authorization", "confirmation_token", "session_path",
})


def _safe_fields(fields: dict[str, Any]) -> dict[str, Any]:
    safe = {k: v for k, v in fields.items() if k not in _SECRET_KEYS}
    aids = safe.get("account_ids")
    if isinstance(aids, list) and len(aids) > 30:
        safe["account_ids_count"] = len(aids)
        safe["account_ids_sample"] = aids[:5]
        del safe["account_ids"]
    return safe


def log_autostory_event(event: str, level: str = "info", **fields: Any) -> None:
    """Concise structured AutoStory lifecycle log (INFO for meaningful events)."""
    safe = _safe_fields(fields)
    log_fn = getattr(logger, str(level).lower(), logger.info)
    log_fn(event, **safe)


def emit_autostory_system_log(
    db,
    event: str,
    *,
    level: str = "INFO",
    account_id: int | None = None,
    **fields: Any,
) -> None:
    """Durable SystemLog row + structured logger (for View Activity)."""
    from src.core.models import SystemLog

    safe = _safe_fields(fields)
    log_autostory_event(event, level=level.lower(), **safe)
    if db is None:
        return
    try:
        db.add(
            SystemLog(
                level=str(level).upper(),
                component="autostory",
                message=event,
                details={"campaign_id": safe.get("campaign_id"), **safe},
                account_id=int(account_id) if account_id is not None else safe.get("account_id"),
            )
        )
        db.flush()
    except Exception as exc:
        logger.warning("autostory_system_log_failed", event=event, error=str(exc))


def get_campaign_activity(db, campaign_id: int, *, limit: int = 80) -> list[dict[str, Any]]:
    from src.core.models import SystemLog

    cid = int(campaign_id)
    rows = (
        db.query(SystemLog)
        .filter(SystemLog.component == "autostory")
        .order_by(SystemLog.id.desc())
        .limit(max(20, min(500, int(limit) * 5)))
        .all()
    )
    out: list[dict[str, Any]] = []
    for row in reversed(rows):
        details = row.details if isinstance(row.details, dict) else {}
        if int(details.get("campaign_id") or 0) != cid:
            continue
        out.append(
            {
                "id": int(row.id),
                "at": row.created_at.isoformat() + "Z" if row.created_at else None,
                "level": row.level,
                "event": row.message,
                "details": details,
                "account_id": row.account_id,
            }
        )
        if len(out) >= int(limit):
            break
    return out[-int(limit) :]
