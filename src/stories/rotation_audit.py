"""Read-only Story Rotation precheck/audit helpers."""
from __future__ import annotations

import random
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from src.ai_agent.account_allowlist import RESERVED_AI_AGENT_ACCOUNT_IDS, scheduler_telethon_excluded_account_ids
from src.core.models import Account, AccountStatus, DiscoveredUser, StoryPool, StoryPoolMember, StoryRun
from src.core.scheduler_models import AccountReadinessSnapshot, JobStatus, ScheduledJob
from src.core.session_paths import account_has_canonical_session
from src.core.safety_policy import get_story_safety_decision
from src.recovery.p9_83_governance_observability import PROTECTED_IDS, PURPOSE_HOLD_IDS
from src.utils.helpers import validate_media

STORY_PURPOSES = frozenset({"", "both", "story", "stories", "autostory"})
RUN_STALE_AFTER_MINUTES = 60
FRESH_STORY_AUTH_TTL_MINUTES = 15
CONTROLLED_LIVE_ACCOUNT_ID = 140


def utcnow() -> datetime:
    return datetime.utcnow()


def parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if hasattr(value, "year"):
        dt = value
    else:
        try:
            raw = str(value).strip()
            if raw.endswith("Z"):
                raw = raw[:-1] + "+00:00"
            dt = datetime.fromisoformat(raw)
        except Exception:
            return None
    if getattr(dt, "tzinfo", None) is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def iso(value: Any) -> str | None:
    dt = parse_dt(value)
    if dt is not None:
        return dt.isoformat()
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def story_purpose_compatible(purpose: str | None) -> bool:
    return (purpose or "both").strip().lower() in STORY_PURPOSES


def classify_story_run(run: StoryRun, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    status = (run.status or "").strip().lower()
    ok = int(run.stories_ok or 0)
    failed = int(run.stories_failed or 0)
    total = ok + failed
    last_activity = parse_dt(run.last_tick_at) or parse_dt(run.started_at) or parse_dt(run.created_at)
    age_minutes = ((now - last_activity).total_seconds() / 60.0) if last_activity else None
    is_stale_running = status == "running" and age_minutes is not None and age_minutes > RUN_STALE_AFTER_MINUTES
    is_pending_empty = status == "pending" and total == 0 and not run.started_at and not run.last_tick_at
    if is_stale_running:
        ui_status = "stale_running"
        hygiene_action = "FAILED_STALE"
    elif is_pending_empty:
        ui_status = "pending_empty"
        hygiene_action = "SKIPPED_EMPTY"
    else:
        ui_status = status or "unknown"
        hygiene_action = None
    return {
        "id": int(run.id),
        "status": status,
        "ui_status": ui_status,
        "hygiene_action": hygiene_action,
        "is_stale_running": is_stale_running,
        "is_pending_empty": is_pending_empty,
        "age_minutes": round(age_minutes, 2) if age_minutes is not None else None,
        "stories_ok": ok,
        "stories_failed": failed,
        "started_at": iso(run.started_at),
        "last_tick_at": iso(run.last_tick_at),
        "created_at": iso(run.created_at),
        "media_path": run.media_path,
        "pool_id": run.pool_id,
        "mode": run.mode,
    }


def _readiness_by_account(db: Session) -> dict[int, AccountReadinessSnapshot]:
    return {int(r.account_id): r for r in db.query(AccountReadinessSnapshot).all()}


def _pool_account_ids(db: Session, pool_id: int | None) -> set[int] | None:
    if not pool_id:
        return None
    return {
        int(r.account_id)
        for r in db.query(StoryPoolMember.account_id).filter(
            StoryPoolMember.pool_id == int(pool_id),
            StoryPoolMember.is_enabled == True,
        )
    }


def media_precheck(media_path: str | None) -> dict[str, Any]:
    path = (media_path or "").strip()
    if not path:
        return {
            "ok": False,
            "required": True,
            "path": None,
            "message": "Media is required before a live story run.",
            "accepted_formats": [".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov"],
            "media_exists": False,
            "media_decodes": False,
            "media_story_compatible": False,
            "media_normalization_required": False,
            "compat_blocker": None,
        }
    ok, err = validate_media(path)
    base = {
        "required": True,
        "path": path,
        "exists": Path(path).exists(),
        "accepted_formats": [".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov"],
    }
    if not ok:
        return {
            **base,
            "ok": False,
            "message": err or "Media validation failed.",
            "media_exists": base["exists"],
            "media_decodes": False,
            "media_story_compatible": False,
            "media_normalization_required": False,
            "compat_blocker": "missing_or_invalid_media",
        }

    # Extension/size passed — add local Story image compatibility (no Telegram calls).
    from src.stories.story_media_compat import evaluate_story_media_compat

    compat = evaluate_story_media_compat(path)
    blocker = compat.get("blocker")
    story_ok = bool(compat.get("ok"))
    message = (
        compat.get("operator_message")
        or ("Media ready." if story_ok else (err or "Media validation failed."))
    )
    return {
        **base,
        "ok": story_ok,
        "message": message,
        "media_exists": bool(compat.get("media_exists")),
        "media_decodes": bool(compat.get("media_decodes")),
        "media_format": compat.get("media_format"),
        "media_width": compat.get("media_width"),
        "media_height": compat.get("media_height"),
        "media_aspect_ratio": compat.get("media_aspect_ratio"),
        "media_vertical": bool(compat.get("media_vertical")),
        "media_color_mode": compat.get("media_color_mode"),
        "media_normalization_required": bool(compat.get("media_normalization_required")),
        "media_story_compatible": bool(compat.get("media_story_compatible")),
        "compat_blocker": blocker,
        "technical_note": compat.get("technical_note"),
    }


def story_auth_is_fresh(account: Account, *, now: datetime | None = None) -> bool:
    from src.stories.story_auth_state import story_auth_is_fresh as _story_auth_is_fresh

    return _story_auth_is_fresh(account, now=now)


def live_gate_allowed(payload: dict[str, Any] | None, precheck: dict[str, Any]) -> tuple[bool, list[str]]:
    payload = payload or {}
    blockers: list[str] = []
    account_ids = precheck.get("eligible_accounts") or []
    if account_ids != [CONTROLLED_LIVE_ACCOUNT_ID]:
        blockers.append("live_gate_requires_account_140_only")
    if payload.get("explicit_operator_approval") is not True:
        blockers.append("explicit_operator_approval_required")
    confirmation = str(payload.get("confirmation_token") or "").strip()
    if confirmation != f"LIVE_STORY_ACCOUNT_{CONTROLLED_LIVE_ACCOUNT_ID}":
        blockers.append("confirmation_token_required")
    if precheck.get("live_blockers"):
        blockers.extend(precheck.get("live_blockers") or [])
    if precheck.get("live_only_blockers"):
        blockers.extend(precheck.get("live_only_blockers") or [])
    return len(blockers) == 0, blockers


def _mention_query(db: Session, *, source_chat_id: int | None, prefer_never_mentioned: bool = True):
    q = db.query(DiscoveredUser).filter(
        DiscoveredUser.is_blocked == False,
        DiscoveredUser.username.isnot(None),
    )
    if prefer_never_mentioned:
        q = q.filter(DiscoveredUser.times_mentioned == 0)
    if source_chat_id is not None:
        q = q.filter(DiscoveredUser.source_chat_id == int(source_chat_id))
    return q


def select_mention_candidates(
    db: Session,
    *,
    source_chat_id: int | None,
    count: int,
    strategy: str = "random",
    prefer_never_mentioned: bool = True,
    mutate: bool = False,
) -> list[dict[str, Any]]:
    """Select mention candidates for precheck/dry-run without mutating mention history."""
    if mutate:
        raise ValueError("mention candidate dry-run selection must not mutate")
    count = max(0, int(count or 0))
    if count == 0:
        return []
    q = _mention_query(
        db,
        source_chat_id=source_chat_id,
        prefer_never_mentioned=prefer_never_mentioned,
    )
    strategy = (strategy or "random").strip().lower()
    if strategy == "oldest":
        rows = q.order_by(DiscoveredUser.discovered_at.asc()).limit(count).all()
    else:
        # Pull a bounded candidate pool and sample in Python to keep SQLite simple.
        pool = q.order_by(DiscoveredUser.discovered_at.asc()).limit(max(count * 20, 100)).all()
        rows = random.sample(pool, min(count, len(pool))) if pool else []
    seen: set[int] = set()
    out: list[dict[str, Any]] = []
    for user in rows:
        uid = int(user.user_id)
        if uid in seen:
            continue
        seen.add(uid)
        out.append(
            {
                "user_id": uid,
                "username": user.username,
                "source_chat_id": user.source_chat_id,
                "source_chat_title": user.source_chat_title,
                "times_mentioned": int(user.times_mentioned or 0),
            }
        )
        if len(out) >= count:
            break
    return out


def mention_precheck(
    db: Session,
    *,
    source_chat_id: int | None,
    mentions_per_story: int,
    estimated_stories: int,
    strategy: str = "random",
    prefer_never_mentioned: bool = True,
) -> dict[str, Any]:
    q = _mention_query(
        db,
        source_chat_id=source_chat_id,
        prefer_never_mentioned=prefer_never_mentioned,
    )
    available = int(q.count())
    required = max(0, int(mentions_per_story or 0) * max(0, int(estimated_stories or 0)))
    sample = select_mention_candidates(
        db,
        source_chat_id=source_chat_id,
        count=min(int(mentions_per_story or 0), 20),
        strategy=strategy,
        prefer_never_mentioned=prefer_never_mentioned,
    )
    return {
        "implemented": True,
        "selection_mode": "random" if (strategy or "random").strip().lower() == "random" else "oldest_never_mentioned",
        "random_selection_implemented": True,
        "avoid_duplicates_per_story": True,
        "prefer_never_mentioned_users": bool(prefer_never_mentioned),
        "dry_run_mutates_history": False,
        "source_chat_id": source_chat_id,
        "mentions_per_story": int(mentions_per_story or 0),
        "available": available,
        "required_for_estimated_run": required,
        "sample_candidates": sample,
        "ok": required == 0 or available >= min(required, int(mentions_per_story or 0)),
        "warning": None if available else "No discovered users available for selected mention source.",
    }


def evaluate_account_story_runtime(
    db: Session,
    account: Account,
    *,
    purpose_filter: str | None = None,
    cooldown_minutes: int = 60,
    max_per_day: int = 1,
    now: datetime | None = None,
    readiness: dict[int, AccountReadinessSnapshot] | None = None,
    excluded: set[int] | None = None,
) -> dict[str, Any]:
    """Per-account story runtime evaluation (read-only; shared by precheck + P10.22 resolver)."""
    now = now or utcnow()
    if readiness is None:
        readiness = _readiness_by_account(db)
    if excluded is None:
        excluded = set(scheduler_telethon_excluded_account_ids(db)) | set(PROTECTED_IDS) | set(PURPOSE_HOLD_IDS)

    aid = int(account.id)
    purpose = (getattr(account, "purpose", None) or "both").strip().lower()
    snap = readiness.get(aid)
    snap_status = (snap.status if snap else None) or None
    snap_expires = parse_dt(snap.expires_at) if snap else None
    snap_ready = bool(snap and snap.status == "READY")
    snap_fresh = bool(snap_ready and (snap_expires is None or snap_expires > now))
    blockers: list[str] = []
    live_only_blockers: list[str] = []
    if aid in PROTECTED_IDS:
        blockers.append("protected_account")
    if aid in PURPOSE_HOLD_IDS:
        blockers.append("held_account")
    if aid in RESERVED_AI_AGENT_ACCOUNT_IDS:
        blockers.append("ai_agent_reserved")
    if aid in excluded and not blockers:
        blockers.append("scheduler_excluded")
    if purpose_filter and purpose != purpose_filter:
        blockers.append(f"purpose_filter:{purpose_filter}")
    if not story_purpose_compatible(purpose):
        blockers.append(f"purpose_not_story_compatible:{purpose}")
    status = str(getattr(getattr(account, "status", None), "value", getattr(account, "status", "")) or "").lower()
    if status != "active":
        blockers.append(f"account_status:{status or 'unknown'}")
    health = (getattr(account, "health_status", None) or "").strip().lower()
    technical_health_ok = health == "alive" or snap_ready
    technical_health_source = "account_health" if health == "alive" else ("v1_readiness" if snap_ready else "none")
    if not technical_health_ok:
        blockers.append(f"health_not_alive:{health or 'unknown'}")
    elif health != "alive":
        live_only_blockers.append(f"stale_account_health_superseded_by_v1_ready:{health or 'unknown'}")
    if not snap_ready:
        blockers.append(f"readiness_not_ready:{snap_status or 'missing'}")
    elif not snap_fresh:
        live_only_blockers.append("readiness_snapshot_expired")
    if not account_has_canonical_session(account):
        blockers.append("missing_canonical_session")
    try:
        decision = get_story_safety_decision(account, requested_action="story_publish")
        if not decision.allowed and not (
            decision.reason_code == "auth_required" and technical_health_ok and health != "alive"
        ):
            blockers.append(decision.reason_code)
        elif not decision.allowed:
            live_only_blockers.append(decision.reason_code)
        next_allowed_at = iso(decision.next_allowed_at)
        safety_reason = decision.reason_code
        safety_human = decision.human_reason
    except Exception as exc:
        blockers.append("story_safety_error")
        next_allowed_at = None
        safety_reason = "story_safety_error"
        safety_human = str(exc)
    last_success = parse_dt(getattr(account, "last_story_success_at", None) or getattr(account, "last_active", None))
    if last_success and (now - last_success) < timedelta(minutes=cooldown_minutes):
        blockers.append("story_cooldown")
    if int(getattr(account, "stories_today", None) or 0) >= max_per_day:
        blockers.append("daily_story_cap")
    from src.stories.story_auth_state import resolve_story_auth_state

    auth_state = resolve_story_auth_state(account, now=now)
    story_precheck_status = auth_state.get("status") or (
        (getattr(account, "story_precheck_status", None) or "").strip().lower() or None
    )
    story_precheck_checked_at = parse_dt(getattr(account, "story_precheck_checked_at", None))
    story_precheck_stale = auth_state.get("state") == "fresh_auth_stale"
    fresh_story_auth_ok = bool(auth_state.get("fresh"))
    auth_live_blocker = (auth_state.get("blockers") or [None])[0]
    if fresh_story_auth_ok:
        live_only_blockers = [
            b
            for b in live_only_blockers
            if not (
                b.startswith("stale_account_health_superseded_by_v1_ready")
                or b == "auth_required"
                or b == "readiness_snapshot_expired"
                or b.startswith("fresh_story_auth_")
            )
        ]
    elif technical_health_ok:
        live_only_blockers = [
            b
            for b in live_only_blockers
            if not (
                b.startswith("stale_account_health_superseded_by_v1_ready")
                or b == "auth_required"
                or b == "readiness_snapshot_expired"
                or b.startswith("fresh_story_auth_")
            )
        ]
        if auth_state.get("state") == "fresh_auth_stale":
            live_only_blockers.append("fresh_story_auth_stale")
        else:
            # Legacy dry-run path: v1 readiness can supersede stale health / old precheck rows.
            live_only_blockers.append("fresh_story_auth_required")
    elif auth_state.get("state") == "failed":
        blockers.extend(auth_state.get("blockers") or [])

    return {
        "account_id": aid,
        "purpose": purpose,
        "status": status,
        "health_status": health or None,
        "technical_health_ok": technical_health_ok,
        "technical_health_source": technical_health_source,
        "readiness_status": snap_status,
        "readiness_fresh": snap_fresh,
        "story_precheck_status": getattr(account, "story_precheck_status", None),
        "story_precheck_checked_at": iso(getattr(account, "story_precheck_checked_at", None)),
        "story_precheck_stale": story_precheck_stale,
        "fresh_story_auth_ok": fresh_story_auth_ok,
        "next_allowed_at": next_allowed_at,
        "safety_reason": safety_reason,
        "safety_human": safety_human,
        "blockers": blockers,
        "live_only_blockers": live_only_blockers,
        "story_ready": not blockers,
        "dry_run_ready": not blockers,
        "live_ready": not blockers and not live_only_blockers and fresh_story_auth_ok,
    }


def build_story_rotation_precheck(db: Session, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    now = utcnow()
    requested_account_ids_raw = payload.get("account_ids") or payload.get("account_id")
    if requested_account_ids_raw in (None, "", "null"):
        requested_account_ids: list[int] | None = None
    elif isinstance(requested_account_ids_raw, list):
        requested_account_ids = [int(x) for x in requested_account_ids_raw if x is not None]
    else:
        requested_account_ids = [int(requested_account_ids_raw)]
    pool_id = payload.get("pool_id")
    pool_id = int(pool_id) if pool_id not in (None, "", "null") else None
    purpose_filter = (payload.get("purpose_filter") or "").strip().lower() or None
    max_accounts = payload.get("max_accounts") or payload.get("max_stories")
    max_accounts = int(max_accounts) if max_accounts not in (None, "", "null") else None
    mentions_per_story = int(payload.get("mentions_per_story") or 0)
    mention_strategy = (payload.get("mention_strategy") or "random").strip().lower()
    cooldown_minutes = int(payload.get("per_account_cooldown_minutes") or 60)
    max_per_day = int(payload.get("max_stories_per_account_per_day") or 1)
    source_chat_id = payload.get("mention_source_chat_id", payload.get("mention_source"))
    source_chat_id = int(source_chat_id) if source_chat_id not in (None, "", "null") else None
    media_path = payload.get("media_path") or payload.get("media_id")

    pool_ids = _pool_account_ids(db, pool_id)
    readiness = _readiness_by_account(db)
    excluded = set(scheduler_telethon_excluded_account_ids(db)) | set(PROTECTED_IDS) | set(PURPOSE_HOLD_IDS)
    accounts_query = db.query(Account).order_by(Account.id)
    if requested_account_ids is not None:
        accounts_query = accounts_query.filter(Account.id.in_(requested_account_ids or [-1]))
    if pool_ids is not None:
        accounts_query = accounts_query.filter(Account.id.in_(pool_ids or {-1}))
    accounts = accounts_query.all()

    rows: list[dict[str, Any]] = []
    blocker_counts: Counter[str] = Counter()
    live_only_blocker_counts: Counter[str] = Counter()
    for account in accounts:
        row = evaluate_account_story_runtime(
            db,
            account,
            purpose_filter=purpose_filter,
            cooldown_minutes=cooldown_minutes,
            max_per_day=max_per_day,
            now=now,
            readiness=readiness,
            excluded=excluded,
        )
        if row["blockers"]:
            blocker_counts.update(row["blockers"])
        if row["live_only_blockers"]:
            live_only_blocker_counts.update(row["live_only_blockers"])
        rows.append(row)
    ready_accounts = [r for r in rows if r["story_ready"]]
    if max_accounts is not None:
        ready_accounts = ready_accounts[:max_accounts]

    missing_requested = []
    if requested_account_ids:
        found = {r["account_id"] for r in rows}
        missing_requested = [aid for aid in requested_account_ids if aid not in found]
        for aid in missing_requested:
            blocker_counts.update(["account_not_found"])

    media = media_precheck(media_path)
    mentions = mention_precheck(
        db,
        source_chat_id=source_chat_id,
        mentions_per_story=mentions_per_story,
        estimated_stories=len(ready_accounts),
        strategy=mention_strategy,
    )
    runs = [classify_story_run(run, now=now) for run in db.query(StoryRun).order_by(StoryRun.id.desc()).limit(25).all()]
    run_counts = Counter(r["ui_status"] for r in runs)
    live_blockers: list[str] = []
    if not media["ok"]:
        compat_blocker = media.get("compat_blocker")
        if compat_blocker in {
            "story_media_not_vertical",
            "story_media_decode_failed",
            "story_media_format_unsupported",
            "story_media_normalization_required",
        }:
            live_blockers.append(str(compat_blocker))
        else:
            live_blockers.append("missing_or_invalid_media")
    if not ready_accounts:
        live_blockers.append("no_story_ready_accounts")
    if mentions_per_story > 0 and mentions["available"] <= 0:
        live_blockers.append("no_mention_candidates")
    if missing_requested:
        live_blockers.append("requested_account_not_found")
    if any(r["is_stale_running"] for r in runs):
        live_blockers.append("stale_story_runs_present")

    pool = db.get(StoryPool, pool_id) if pool_id else None
    account_blockers = {str(r["account_id"]): r["blockers"] for r in rows if r["blockers"]}
    account_live_only_blockers = {
        str(r["account_id"]): r["live_only_blockers"]
        for r in rows
        if r["live_only_blockers"]
    }
    selected_mentions = select_mention_candidates(
        db,
        source_chat_id=source_chat_id,
        count=mentions_per_story,
        strategy=mention_strategy,
    )
    live_only_blockers = sorted(set(live_only_blocker_counts.elements()))
    live_publish_allowed, live_gate_blockers = live_gate_allowed(
        payload,
        {
            "eligible_accounts": [r["account_id"] for r in ready_accounts],
            "live_blockers": live_blockers,
            "live_only_blockers": live_only_blockers,
        },
    )
    return {
        "ok": not live_blockers,
        "dry_run": True,
        "live_run_allowed": live_publish_allowed,
        "live_publish_allowed": live_publish_allowed,
        "requires_operator_approval": True,
        "live_gate_blockers": live_gate_blockers,
        "controlled_live_account_id": CONTROLLED_LIVE_ACCOUNT_ID,
        "live_blockers": live_blockers,
        "live_only_blockers": live_only_blockers,
        "account_blockers": account_blockers,
        "account_live_only_blockers": account_live_only_blockers,
        "eligible_accounts": [r["account_id"] for r in ready_accounts],
        "blocked_accounts": [r["account_id"] for r in rows if r["blockers"]],
        "requested_account_ids": requested_account_ids,
        "missing_requested_account_ids": missing_requested,
        "checked_at": now.isoformat(),
        "settings": {
            "pool_id": pool_id,
            "pool_name": pool.name if pool else None,
            "pool_optional": True,
            "all_ready_accounts_mode": pool_id is None,
            "purpose_filter": purpose_filter,
            "per_account_cooldown_minutes": cooldown_minutes,
            "max_stories_per_account_per_day": max_per_day,
            "max_accounts": max_accounts,
            "mention_strategy": mention_strategy,
        },
        "counts": {
            "accounts_considered": len(rows),
            "story_ready": len(ready_accounts),
            "eligible_but_blocked": len(rows) - len(ready_accounts),
            "cooldown_blocked": blocker_counts.get("story_cooldown", 0),
            "rate_limited": sum(v for k, v in blocker_counts.items() if "rate_limited" in k),
            "frozen_or_blocked": sum(v for k, v in blocker_counts.items() if "frozen" in k or "blocked" in k),
            "missing_media_or_config": 0 if media["ok"] else 1,
            "active_or_stale_runs": run_counts.get("running", 0) + run_counts.get("pending", 0) + run_counts.get("stale_running", 0) + run_counts.get("pending_empty", 0),
            "mention_candidates": mentions["available"],
        },
        "blocker_counts": dict(blocker_counts.most_common()),
        "live_only_blocker_counts": dict(live_only_blocker_counts.most_common()),
        "ready_account_ids": [r["account_id"] for r in ready_accounts],
        "sample_accounts": rows[:25],
        "accounts": rows,
        "media": media,
        "mentions": mentions,
        "selected_mention_candidates": selected_mentions,
        "runs": runs,
        "run_counts": dict(run_counts),
    }


def build_story_dry_run_plan(db: Session, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a realistic no-publish execution plan from the precheck result."""
    precheck = build_story_rotation_precheck(db, {**(payload or {}), "dry_run": True})
    eligible_ids = list(precheck.get("eligible_accounts") or [])
    max_stories = (payload or {}).get("max_stories") or (payload or {}).get("max_accounts") or len(eligible_ids)
    max_stories = int(max_stories or 0)
    selected_ids = eligible_ids[:max_stories] if max_stories > 0 else eligible_ids
    mentions_per_story = int((payload or {}).get("mentions_per_story") or 0)
    mention_candidates = list(precheck.get("selected_mention_candidates") or [])
    execution_order = []
    for idx, account_id in enumerate(selected_ids, start=1):
        execution_order.append(
            {
                "step": idx,
                "account_id": account_id,
                "media_path": precheck["media"].get("path"),
                "caption": (payload or {}).get("caption"),
                "mentions": mention_candidates[:mentions_per_story],
                "cooldown_minutes_after_success": precheck["settings"]["per_account_cooldown_minutes"],
                "would_publish": False,
            }
        )
    return {
        "ok": bool(precheck["ok"]),
        "dry_run": True,
        "precheck": precheck,
        "selected_accounts": selected_ids,
        "selected_media": precheck["media"],
        "caption": (payload or {}).get("caption"),
        "mention_strategy": precheck["settings"]["mention_strategy"],
        "selected_mention_candidates": mention_candidates,
        "story_count": len(execution_order),
        "execution_order": execution_order,
        "cooldown": {
            "per_account_minutes": precheck["settings"]["per_account_cooldown_minutes"],
            "max_stories_per_account_per_day": precheck["settings"]["max_stories_per_account_per_day"],
        },
        "safety_gates": {
            "live_publish_allowed": False,
            "requires_operator_approval": True,
            "live_blockers": precheck.get("live_blockers", []),
            "live_only_blockers": precheck.get("live_only_blockers", []),
            "dry_run_does_not_publish": True,
            "dry_run_does_not_create_story_rows": True,
        },
    }
