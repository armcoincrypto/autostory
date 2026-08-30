"""MODEL A AutoStory hardening: durable claim, wave cap, campaign auth, progress.

Fails closed. Does not replace STORY_MUTATIONS_ENABLED.
"""
from __future__ import annotations

import os
import socket
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

import structlog
from sqlalchemy import and_, or_, text
from sqlalchemy.orm import Session

logger = structlog.get_logger(__name__)

MAX_AUTOSTORY_WAVE_SIZE = int(os.environ.get("MAX_AUTOSTORY_WAVE_SIZE") or "25")
CLAIM_LEASE_MINUTES = int(os.environ.get("AUTOSTORY_CLAIM_LEASE_MINUTES") or "30")
PROGRESS_TERMINAL_SKIP = frozenset(
    {"reconciled", "published", "failed", "deferred", "ambiguous", "ok", "skipped"}
)
PROGRESS_NO_RESEND = frozenset({"reconciled", "published", "ambiguous", "ok"})


async def ensure_fresh_story_auth_for_accounts(
    account_ids: list[int],
) -> dict[str, Any]:
    """Read-only CanSendStory refresh for accounts whose precheck is past TTL.

    Does not weaken the 15-minute freshness requirement. Called at wave
    execution time so scheduled campaigns do not trust create-time probes.
    """
    from src.core.database import get_db_context
    from src.core.models import Account
    from src.stories.client_lifecycle import open_controlled_story_client
    from src.stories.precheck import persist_precheck_result, run_story_precheck
    from src.stories.story_auth_state import resolve_story_auth_state, story_auth_is_fresh

    refreshed: list[int] = []
    already_fresh: list[int] = []
    failed: list[dict[str, Any]] = []

    for aid in [int(x) for x in account_ids]:
        with get_db_context() as db:
            acc = db.get(Account, aid)
            if acc is None:
                failed.append({"account_id": aid, "error": "account_not_found"})
                continue
            if story_auth_is_fresh(acc):
                already_fresh.append(aid)
                continue
            # story_auth_is_fresh() is only ever True for a fresh "allowed" result --
            # a known cooldown (story_blocked_until in the future, e.g. from a prior
            # STORIES_TOO_MUCH/FloodWait precheck) is NOT "fresh" but must still skip
            # the remote call: attempting a known-blocked account again is pointless
            # and, at the scheduler's 45s base tick, becomes a tight retry loop that
            # hammers Telegram (confirmed in production, Campaign #18). Reuse the
            # durable story_blocked_until field rather than a new mechanism.
            auth_state = resolve_story_auth_state(acc, now=datetime.utcnow())
            if auth_state.get("state") == "blocked":
                failed.append(
                    {
                        "account_id": aid,
                        "error": "story_auth_blocked_until_future",
                        "reason": auth_state.get("reason") or "",
                        "blocked_until": auth_state.get("blocked_until"),
                        "checked_at": auth_state.get("checked_at"),
                    }
                )
                continue

        lease, err = await open_controlled_story_client(aid)
        if lease is None:
            failed.append({"account_id": aid, "error": err or "client_unavailable"})
            continue
        try:
            result = await run_story_precheck(lease.wrapper.client, aid)
            status = str(result.get("status") or "").lower()
            if result.get("allowed") is True or status in {"allowed", "ok"}:
                status = "allowed"
            reason = str(result.get("reason") or result.get("error") or "")[:200]
            persist_precheck_result(
                aid, status or "failed_check", reason, result.get("retry_after_seconds")
            )
            if status != "allowed":
                retry_after = result.get("retry_after_seconds")
                blocked_until_iso = (
                    (datetime.utcnow() + timedelta(seconds=int(retry_after))).isoformat()
                    if retry_after
                    else None
                )
                failed.append(
                    {
                        "account_id": aid,
                        "error": f"precheck_{status}",
                        "reason": reason,
                        "blocked_until": blocked_until_iso,
                        "checked_at": datetime.utcnow().isoformat(),
                    }
                )
            else:
                refreshed.append(aid)
        except Exception as exc:
            failed.append({"account_id": aid, "error": f"{type(exc).__name__}: {exc}"})
        finally:
            await lease.close()

    out = {
        "already_fresh": already_fresh,
        "refreshed": refreshed,
        "failed": failed,
        "ok": len(failed) == 0,
    }
    logger.info("autostory_fresh_auth_gate", **out)
    return out


def worker_identity() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def wave_size_ok(account_ids: list[int]) -> tuple[bool, str | None]:
    if len(account_ids) > MAX_AUTOSTORY_WAVE_SIZE:
        return False, "WAVE_SIZE_EXCEEDS_MAX"
    return True, None


def is_account_certified_publish(db: Session, account_id: int) -> tuple[bool, str]:
    """Fail-closed: durable controlled-publish evidence == CERTIFIED_PUBLISH."""
    from src.core.account_protection import PROTECTED_IDS, PURPOSE_HOLD_IDS
    from src.core.models import Account
    from src.ai_agent.account_allowlist import RESERVED_AI_AGENT_ACCOUNT_IDS
    from src.stories.fleet_certification import durable_certification_evidence

    aid = int(account_id)
    if aid in PROTECTED_IDS or aid in PURPOSE_HOLD_IDS:
        return False, "account_protected"
    if aid in RESERVED_AI_AGENT_ACCOUNT_IDS:
        return False, "account_reserved"

    acc = db.get(Account, aid)
    if acc is None:
        return False, "account_not_found"
    status = acc.status
    status_s = (status.value if hasattr(status, "value") else str(status or "")).lower()
    if status_s in {"disabled", "banned", "deleted", "frozen"}:
        return False, "account_disabled"

    evidence = durable_certification_evidence(db)
    if aid not in evidence:
        return False, "account_not_certified_publish"
    return True, "certified_publish"


def account_has_daily_capacity(db: Session, account_id: int) -> tuple[bool, str]:
    from src.core.models import Account
    from src.stories.autostory_recurring import platform_daily_limit
    from src.stories.daily_story_counter import ensure_stories_today_current

    acc = db.get(Account, int(account_id))
    if acc is None:
        return False, "account_not_found"
    count = ensure_stories_today_current(acc)
    limit = platform_daily_limit(acc)
    if count >= limit:
        return False, "daily_capacity_exhausted"
    return True, "capacity_ok"


def last_successful_story_at(db: Session, account_id: int) -> Optional[datetime]:
    from src.core.models import Account, Story

    acc = db.get(Account, int(account_id))
    for attr in ("last_story_success_at", "last_successful_story_at"):
        val = getattr(acc, attr, None) if acc is not None else None
        if val is not None:
            return val
    latest = (
        db.query(Story)
        .filter(Story.account_id == int(account_id), Story.is_deleted.is_(False))
        .order_by(Story.published_at.desc().nullslast(), Story.id.desc())
        .first()
    )
    if latest is None:
        return None
    return getattr(latest, "published_at", None)


def rotate_account_ids(db: Session, account_ids: list[int]) -> list[int]:
    """Deterministic LRU: least-recent successful Story first; never-published first."""
    decorated = []
    for aid in account_ids:
        ts = last_successful_story_at(db, int(aid))
        decorated.append((ts is not None, ts or datetime.min, int(aid)))
    decorated.sort(key=lambda t: (t[0], t[1], t[2]))
    return [aid for _, __, aid in decorated]


def claim_campaign(
    db: Session,
    campaign_id: int,
    *,
    worker_id: str | None = None,
    now: datetime | None = None,
    lease_minutes: int | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Atomic durable claim. Returns (won, meta)."""
    from src.core.models import AutoStoryCampaign

    now = now or datetime.utcnow()
    worker_id = worker_id or worker_identity()
    lease = int(lease_minutes if lease_minutes is not None else CLAIM_LEASE_MINUTES)
    expires = now + timedelta(minutes=lease)

    # SQLite-safe compare-and-set via filtered UPDATE rowcount
    result = db.execute(
        text(
            """
            UPDATE auto_story_campaigns
            SET claimed_by = :worker,
                claimed_at = :now,
                claim_expires_at = :expires,
                updated_at = :now
            WHERE id = :cid
              AND status = 'active'
              AND (
                    claimed_by IS NULL
                 OR claim_expires_at IS NULL
                 OR claim_expires_at < :now
              )
            """
        ),
        {"worker": worker_id, "now": now, "expires": expires, "cid": int(campaign_id)},
    )
    db.commit()
    won = int(result.rowcount or 0) == 1
    c = db.get(AutoStoryCampaign, int(campaign_id))
    meta = {
        "won": won,
        "campaign_id": int(campaign_id),
        "claimed_by": getattr(c, "claimed_by", None) if c else None,
        "claimed_at": getattr(c, "claimed_at", None).isoformat() if c and getattr(c, "claimed_at", None) else None,
        "claim_expires_at": (
            getattr(c, "claim_expires_at", None).isoformat()
            if c and getattr(c, "claim_expires_at", None)
            else None
        ),
        "worker_id": worker_id,
    }
    if won:
        try:
            from src.stories.autostory_operator_preview import emit_autostory_system_log

            emit_autostory_system_log(
                db,
                "autostory.campaign.claimed",
                campaign_id=int(campaign_id),
                worker_id=worker_id,
                claimed_at=meta.get("claimed_at"),
                story_run_id=getattr(c, "last_story_run_id", None) if c else None,
                account_count=len(list(getattr(c, "account_ids", None) or [])) if c else None,
                scheduled_at=c.next_wave_at.isoformat() if c and c.next_wave_at else None,
            )
            db.commit()
        except Exception:
            logger.info("autostory.campaign.claimed", **meta)
    else:
        logger.debug("autostory.campaign.claim_miss", **meta)
    return won, meta


def release_campaign_claim(
    db: Session,
    campaign_id: int,
    *,
    worker_id: str | None = None,
    force: bool = False,
    now: datetime | None = None,
) -> None:
    from src.core.models import AutoStoryCampaign

    now = now or datetime.utcnow()
    c = db.get(AutoStoryCampaign, int(campaign_id))
    if c is None:
        return
    if not force and worker_id and c.claimed_by and c.claimed_by != worker_id:
        return
    c.claimed_by = None
    c.claimed_at = None
    c.claim_expires_at = None
    c.updated_at = now
    db.commit()


def authorize_wave(
    db: Session,
    campaign_id: int,
    account_ids: list[int],
    *,
    wave_index: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Grant durable mutation scope to exactly these accounts for this wave."""
    from src.core.models import AutoStoryCampaign

    now = now or datetime.utcnow()
    ids = [int(x) for x in account_ids]
    ok, err = wave_size_ok(ids)
    if not ok:
        raise ValueError(err or "WAVE_SIZE_EXCEEDS_MAX")

    c = db.get(AutoStoryCampaign, int(campaign_id))
    if c is None:
        raise ValueError("campaign_not_found")

    # Account-level exclusive locks
    for aid in ids:
        existing = db.execute(
            text(
                "SELECT campaign_id FROM auto_story_account_locks WHERE account_id = :aid"
            ),
            {"aid": aid},
        ).fetchone()
        if existing and int(existing[0]) != int(campaign_id):
            raise ValueError(f"account_locked_by_other_campaign:{aid}:{existing[0]}")
        db.execute(
            text(
                """
                INSERT INTO auto_story_account_locks (account_id, campaign_id, wave_index, locked_at, expires_at)
                VALUES (:aid, :cid, :wave, :now, :exp)
                ON CONFLICT(account_id) DO UPDATE SET
                  campaign_id=excluded.campaign_id,
                  wave_index=excluded.wave_index,
                  locked_at=excluded.locked_at,
                  expires_at=excluded.expires_at
                """
            ),
            {
                "aid": aid,
                "cid": int(campaign_id),
                "wave": int(wave_index),
                "now": now,
                "exp": now + timedelta(minutes=CLAIM_LEASE_MINUTES),
            },
        )

    c.authorized_account_ids = ids
    c.authorization_wave_index = int(wave_index)
    c.authorization_revoked_at = None
    c.updated_at = now
    db.commit()
    return {
        "campaign_id": int(campaign_id),
        "wave_index": int(wave_index),
        "authorized_account_ids": ids,
    }


def revoke_wave_authorization(
    db: Session,
    campaign_id: int,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Canonical cleanup: clear wave scope + account locks for this campaign."""
    from src.core.models import AutoStoryCampaign

    now = now or datetime.utcnow()
    c = db.get(AutoStoryCampaign, int(campaign_id))
    prev = list(getattr(c, "authorized_account_ids", None) or []) if c else []
    if c is not None:
        c.authorized_account_ids = []
        c.authorization_revoked_at = now
        c.updated_at = now
    db.execute(
        text("DELETE FROM auto_story_account_locks WHERE campaign_id = :cid"),
        {"cid": int(campaign_id)},
    )
    db.commit()
    from src.stories.autostory_operator_preview import emit_autostory_system_log

    emit_autostory_system_log(
        db,
        "autostory.authorization.revoked",
        campaign_id=int(campaign_id),
        active_authorizations=0,
        previously_authorized_count=len(prev),
    )
    db.commit()
    return {"campaign_id": int(campaign_id), "revoked_account_ids": prev}


def account_in_active_wave_authorization(account_id: int) -> bool:
    """DB lookup: account currently in a non-revoked authorized wave."""
    from src.core.database import get_db_context
    from src.core.models import AutoStoryCampaign

    aid = int(account_id)
    try:
        with get_db_context() as db:
            rows = (
                db.query(AutoStoryCampaign)
                .filter(
                    AutoStoryCampaign.status == "active",
                    AutoStoryCampaign.authorization_revoked_at.is_(None),
                )
                .all()
            )
            for c in rows:
                auth = list(getattr(c, "authorized_account_ids", None) or [])
                if aid in {int(x) for x in auth}:
                    return True
            lock = db.execute(
                text("SELECT 1 FROM auto_story_account_locks WHERE account_id = :aid"),
                {"aid": aid},
            ).fetchone()
            return lock is not None
    except Exception as exc:
        logger.warning("wave_auth_lookup_failed", account_id=aid, error=str(exc))
        return False


def ensure_progress_row(
    db: Session,
    *,
    campaign_id: int,
    wave_index: int,
    account_id: int,
    status: str = "pending",
) -> Any:
    from src.core.models import AutoStoryAccountProgress

    row = (
        db.query(AutoStoryAccountProgress)
        .filter_by(
            campaign_id=int(campaign_id),
            wave_index=int(wave_index),
            account_id=int(account_id),
        )
        .one_or_none()
    )
    if row is None:
        row = AutoStoryAccountProgress(
            campaign_id=int(campaign_id),
            wave_index=int(wave_index),
            account_id=int(account_id),
            status=status,
        )
        db.add(row)
        db.flush()
    return row


def load_durable_mention_plan(
    db: Session,
    *,
    campaign_id: int,
    wave_index: int,
    account_ids: list[int],
) -> dict[int, list[dict]] | None:
    """Return the previously-persisted per-account mention plan for this wave slot.

    Returns None unless *every* requested account already has a durably
    stored plan (mention_plan is not None) for this exact (campaign_id,
    wave_index) slot -- i.e. a prior attempt already selected targets and a
    retry must reuse them rather than re-randomize. A stored empty list
    (mentions_per_story == 0) still counts as decided and is reused.
    """
    from src.core.models import AutoStoryAccountProgress

    if not account_ids:
        return None
    rows = (
        db.query(AutoStoryAccountProgress)
        .filter(
            AutoStoryAccountProgress.campaign_id == int(campaign_id),
            AutoStoryAccountProgress.wave_index == int(wave_index),
            AutoStoryAccountProgress.account_id.in_([int(a) for a in account_ids]),
        )
        .all()
    )
    by_account = {int(r.account_id): r for r in rows}
    plan: dict[int, list[dict]] = {}
    for aid in account_ids:
        row = by_account.get(int(aid))
        if row is None or row.mention_plan is None:
            return None
        plan[int(aid)] = list(row.mention_plan)
    return plan


def persist_mention_plan_for_wave(
    db: Session,
    *,
    campaign_id: int,
    wave_index: int,
    per_account_plan: dict[int, list[dict]],
) -> None:
    """Durably store each account's selected mention targets for this wave slot.

    Called once, immediately after a fresh selection and before publish is
    attempted, so a crash/retry of the same slot finds and reuses the
    already-selected targets (see load_durable_mention_plan) instead of
    drawing a new random sample. Never overwrites a slot that is already
    decided (mention_plan is not None) -- defensive even though the normal
    caller only reaches here when load_durable_mention_plan already
    confirmed the whole account_ids set is undecided, this guarantees an
    already-committed slot can never be clobbered if that set ever shifts.
    """
    for aid, chunk in per_account_plan.items():
        row = ensure_progress_row(
            db, campaign_id=campaign_id, wave_index=wave_index, account_id=int(aid)
        )
        if row.mention_plan is not None:
            continue
        row.mention_plan = list(chunk)
    db.commit()


def update_progress(
    db: Session,
    *,
    campaign_id: int,
    wave_index: int,
    account_id: int,
    status: str,
    story_id: int | None = None,
    telegram_story_id: int | None = None,
    run_id: int | None = None,
    error: str | None = None,
) -> None:
    row = ensure_progress_row(
        db,
        campaign_id=campaign_id,
        wave_index=wave_index,
        account_id=account_id,
    )
    # Never overwrite terminal reconciled / ambiguous with attempting
    if row.status in PROGRESS_NO_RESEND and status in ("pending", "claimed", "attempting"):
        return
    row.status = status
    row.attempt_count = int(row.attempt_count or 0) + (1 if status == "attempting" else 0)
    if story_id is not None:
        row.story_id = int(story_id)
    if telegram_story_id is not None:
        row.telegram_story_id = int(telegram_story_id)
    if run_id is not None:
        row.run_id = int(run_id)
    if error is not None:
        row.error = str(error)[:2000]
    row.updated_at = datetime.utcnow()
    if status in ("published", "reconciled", "ok"):
        row.reconciled_at = datetime.utcnow()
        if status == "published":
            row.status = "reconciled"
    db.commit()


def progress_status(
    db: Session, *, campaign_id: int, wave_index: int, account_id: int
) -> str | None:
    from src.core.models import AutoStoryAccountProgress

    row = (
        db.query(AutoStoryAccountProgress)
        .filter_by(
            campaign_id=int(campaign_id),
            wave_index=int(wave_index),
            account_id=int(account_id),
        )
        .one_or_none()
    )
    return str(row.status) if row else None


def recover_stale_attempting(
    db: Session,
    *,
    campaign_id: int,
    wave_index: int,
    account_id: int,
    now: datetime | None = None,
) -> str | None:
    """Resolve a stale in-flight ``attempting`` wave slot to a durable, no-resend state.

    Covers the crash window a caught exception cannot: if the worker process
    is killed outright between Telegram accepting a Story send and any local
    code running (not a Python exception -- publisher.py's own
    ``telegram_accepted`` except-branch already classifies *that* case as
    ``ambiguous_no_retry``), the durable row is left at ``attempting`` with
    no story_id, no run linkage, and no other identifying evidence of the
    outcome. No Telegram-side lookup capability exists for Stories today
    (unlike the P5D message-gateway's live reconciliation lookup) to prove
    the send was published or never sent, so ``ambiguous`` -- fail closed,
    no resend -- is the only classification the available evidence
    supports. ``reconcile_daily_from_wave_slot`` (already called immediately
    after this in both selection paths) then reflects the transition into
    ``AutoStoryDailyProgress`` using its existing idempotent logic.

    Staleness reuses ``CLAIM_LEASE_MINUTES`` -- the same TTL that already
    bounds campaign claims and account locks -- instead of a second timer:
    ``attempting`` is written immediately after a wave claim is won, so once
    a full lease period has elapsed since the row's last write, the worker
    that wrote it can no longer legitimately hold that claim, confirming it
    is gone (crashed, not merely slow). This is a pure, row-local, evidence
    age check -- it does not require the caller to currently hold the
    campaign claim, so it is safe to call from read-only preview/dry-run
    selection paths as well as live wave execution.

    Idempotent: a row already resolved away from ``attempting`` is a no-op.
    Returns the resulting status, or ``None`` if there was nothing to do.
    """
    from src.core.models import AutoStoryAccountProgress

    now = now or datetime.utcnow()
    row = (
        db.query(AutoStoryAccountProgress)
        .filter_by(
            campaign_id=int(campaign_id),
            wave_index=int(wave_index),
            account_id=int(account_id),
        )
        .one_or_none()
    )
    if row is None or str(row.status) != "attempting":
        return None

    age_minutes = (now - row.updated_at).total_seconds() / 60.0 if row.updated_at else None
    if age_minutes is None or age_minutes < CLAIM_LEASE_MINUTES:
        return None  # still within the window a legitimate worker may own this

    update_progress(
        db,
        campaign_id=int(campaign_id),
        wave_index=int(wave_index),
        account_id=int(account_id),
        status="ambiguous",
        error="stale_attempting_no_evidence_of_outcome",
    )
    logger.warning(
        "autostory.progress.stale_attempting_recovered",
        campaign_id=int(campaign_id),
        wave_index=int(wave_index),
        account_id=int(account_id),
        age_minutes=round(age_minutes, 1),
    )
    return "ambiguous"


def select_next_wave_accounts(
    db: Session,
    campaign: Any,
    *,
    wave_index: int,
) -> dict[str, Any]:
    """Capacity + cert aware selection of up to MAX accounts not yet terminal.

    Legacy ``accounts_publish_once``: one success permanently completes the account.
    ``recurring_daily``: delegates to day-scoped remaining targets.
    """
    from src.stories.autostory_recurring import is_recurring, select_next_recurring_wave_accounts

    if is_recurring(campaign):
        return select_next_recurring_wave_accounts(db, campaign, wave_index=wave_index)

    from src.core.models import AutoStoryAccountProgress

    all_ids = [int(x) for x in (campaign.account_ids or [])]
    rotated = rotate_account_ids(db, all_ids)

    done_ids: set[int] = set()
    prior = (
        db.query(AutoStoryAccountProgress)
        .filter(AutoStoryAccountProgress.campaign_id == int(campaign.id))
        .all()
    )
    for row in prior:
        status = str(row.status)
        if status == "attempting":
            recovered = recover_stale_attempting(
                db,
                campaign_id=int(campaign.id),
                wave_index=int(row.wave_index),
                account_id=int(row.account_id),
            )
            if recovered is not None:
                status = recovered
        if status in PROGRESS_NO_RESEND or status in ("failed", "deferred"):
            # deferred may retry next day — exclude from this wave index only if same wave done
            if status in PROGRESS_NO_RESEND:
                done_ids.add(int(row.account_id))
            elif status == "deferred" and int(row.wave_index) == int(wave_index):
                done_ids.add(int(row.account_id))

    eligible: list[int] = []
    blocked: list[dict[str, Any]] = []
    for aid in rotated:
        if aid in done_ids:
            continue
        cert_ok, cert_reason = is_account_certified_publish(db, aid)
        if not cert_ok:
            blocked.append({"account_id": aid, "reason": cert_reason})
            continue
        cap_ok, cap_reason = account_has_daily_capacity(db, aid)
        if not cap_ok:
            blocked.append({"account_id": aid, "reason": cap_reason})
            ensure_progress_row(
                db, campaign_id=int(campaign.id), wave_index=wave_index, account_id=aid, status="deferred"
            )
            continue
        eligible.append(aid)
        if len(eligible) >= MAX_AUTOSTORY_WAVE_SIZE:
            break

    remaining_after = [
        aid for aid in rotated if aid not in done_ids and aid not in eligible
        and aid not in {b["account_id"] for b in blocked if b["reason"] == "daily_capacity_exhausted"}
    ]
    # recount remaining unfinished certified
    unfinished = [aid for aid in rotated if aid not in done_ids]

    return {
        "wave_account_ids": eligible,
        "blocked": blocked,
        "unfinished_count": len(unfinished),
        "remaining_after_wave": max(0, len(unfinished) - len(eligible)),
        "wave_index": int(wave_index),
        "max_wave_size": MAX_AUTOSTORY_WAVE_SIZE,
        "recurring": False,
    }


def plan_full_fleet_waves(db: Session, account_ids: list[int]) -> dict[str, Any]:
    """Non-live simulation: unique bounded waves with rotation."""
    rotated = rotate_account_ids(db, [int(x) for x in account_ids])
    waves: list[list[int]] = []
    for i in range(0, len(rotated), MAX_AUTOSTORY_WAVE_SIZE):
        waves.append(rotated[i : i + MAX_AUTOSTORY_WAVE_SIZE])
    flat = [a for w in waves for a in w]
    return {
        "account_count": len(rotated),
        "wave_count": len(waves),
        "waves": [{"index": i + 1, "size": len(w), "account_ids": w} for i, w in enumerate(waves)],
        "max_wave_size": MAX_AUTOSTORY_WAVE_SIZE,
        "duplicates": len(flat) - len(set(flat)),
        "all_unique": len(flat) == len(set(flat)),
        "any_over_max": any(len(w) > MAX_AUTOSTORY_WAVE_SIZE for w in waves),
    }
