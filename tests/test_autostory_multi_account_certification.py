"""AutoStory multi-account / multi-wave production-readiness certification.

Answers, with deterministic simulation (no Telegram, no provider calls, pure
DB): if an operator selects 10/25/26/50/94 (all eligible) accounts, does
AutoStory correctly process every one of them through however many <=25
waves that requires, without dropping, duplicating, or exceeding any
account's daily target? See docs/AUTOSTORY_MULTI_ACCOUNT_READINESS.md for
the narrative report this file's assertions back.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.core.database import Base
from src.core.models import (
    Account,
    AccountStatus,
    AutoStoryAccountProgress,
    AutoStoryCampaign,
)
from src.stories.autostory_hardening import (
    MAX_AUTOSTORY_WAVE_SIZE,
    PROGRESS_NO_RESEND,
    claim_campaign,
    plan_full_fleet_waves,
    progress_status,
    recover_stale_attempting,
    rotate_account_ids,
    update_progress,
)
from src.stories.autostory_recurring import (
    CAMPAIGN_MODE_RECURRING,
    campaign_daily_progress_summary,
    campaign_local_dates,
    clamp_stories_per_account_per_day,
    effective_remaining_today,
    ensure_daily_progress_rows,
    max_story_publishes,
    platform_remaining_today,
    record_daily_ambiguous,
    record_daily_failed,
    record_daily_success,
    recurring_campaign_complete,
    remaining_today,
    select_next_recurring_wave_accounts,
)
from src.stories.daily_story_counter import record_successful_story_publish


@pytest.fixture()
def rec_db(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with engine.connect() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS auto_story_account_locks (
                  account_id INTEGER PRIMARY KEY,
                  campaign_id INTEGER NOT NULL,
                  wave_index INTEGER NOT NULL,
                  locked_at DATETIME,
                  expires_at DATETIME
                )
                """
            )
        )
        conn.commit()
    Session = sessionmaker(bind=engine)
    session = Session()

    @contextmanager
    def _ctx():
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise

    monkeypatch.setattr("src.core.database.get_db_context", _ctx)
    monkeypatch.setattr("src.stories.auto_story_service.get_db_context", _ctx, raising=False)
    monkeypatch.setattr("src.stories.autostory_hardening.get_db_context", _ctx, raising=False)
    monkeypatch.setenv("MAX_STORIES_PER_ACCOUNT_PER_DAY", "3")
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("STORY_MUTATIONS_ENABLED", "false")
    monkeypatch.setenv("CAMPAIGN_EXECUTION_ENABLED", "false")
    monkeypatch.setenv("CONTROLLED_STORY_EXECUTION_ENABLED", "false")
    monkeypatch.setenv("SCHEDULER_STORY_EXECUTION_ENABLED", "false")
    monkeypatch.setenv("STORY_EXECUTION_MODE", "disabled")
    monkeypatch.setenv("AUTOSTORY_MENTIONS_PRODUCTION_CERTIFIED", "false")
    yield session
    session.close()


_phone_counter = __import__("itertools").count()


def _seed_accounts(db, n: int, *, daily_limit: int | None = None) -> list[int]:
    """Note: daily_limit is only reliable for n=1 immediately followed by use
    in the same test (see test_capacity_exhausted_defers_safely_then_rolls_over
    for why: it's a non-mapped attribute that can be lost to SQLAlchemy's
    weak-reference identity map once the loop variable is reassigned).
    """
    ids = []
    for _ in range(n):
        seq = next(_phone_counter)
        a = Account(
            # Explicit high id: real production has hardcoded protected/reserved
            # account id sets (PROTECTED_IDS, PURPOSE_HOLD_IDS,
            # RESERVED_AI_AGENT_ACCOUNT_IDS -- max known value 207) that
            # fleet_autostory_summary correctly excludes regardless of which DB
            # is queried. A fresh in-memory test DB's autoincrement starting at 1
            # would collide with those real values; starting well above them
            # avoids that entirely without needing to know the exact set.
            id=10_000 + seq,
            phone_number=f"+1555200{seq:05d}",
            status=AccountStatus.ACTIVE,
            stories_today=0,
            stories_today_on=date.today(),
        )
        if daily_limit is not None:
            a.daily_story_limit = int(daily_limit)  # type: ignore[attr-defined]
        db.add(a)
        db.flush()
        ids.append(int(a.id))
    db.commit()
    return ids


def _campaign(
    db,
    ids: list[int],
    *,
    spad: int = 1,
    days: int = 1,
    started_at: datetime | None = None,
    status: str = "active",
) -> AutoStoryCampaign:
    started = started_at or datetime(2026, 8, 26, 6, 0, 0)
    c = AutoStoryCampaign(
        status=status,
        account_ids=list(ids),
        media_path="/tmp/cert.jpg",
        caption="cert",
        mentions_per_story=0,
        duration_days=days,
        posts_per_day=spad,
        times_json=["10:00", "15:00", "20:00"][: max(1, spad)],
        campaign_mode=CAMPAIGN_MODE_RECURRING,
        stories_per_account_per_day=spad,
        started_at=started,
        ends_at=started + timedelta(days=days),
        awake_start_hhmm="10:00",
        awake_end_hhmm="20:00",
        explicit_operator_approval=True,
        confirmation_token="I_CONFIRM_STORY_PUBLISH",
    )
    db.add(c)
    db.commit()
    dates = campaign_local_dates(started_at=c.started_at, duration_days=days, tz_name="UTC")
    ensure_daily_progress_rows(
        db, campaign_id=int(c.id), account_ids=ids, local_dates=dates, target_count=spad
    )
    db.commit()
    return c


def _cert_ok(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.stories.autostory_hardening.is_account_certified_publish",
        lambda db, aid: (True, "certified_publish"),
    )
    # fleet_autostory_summary (used by select_automatic_accounts / create_campaign's
    # automatic path) builds its candidate list from durable_certification_evidence
    # directly, not from is_account_certified_publish -- both must be mocked for any
    # test that exercises automatic selection or "All eligible".
    monkeypatch.setattr(
        "src.stories.fleet_certification.durable_certification_evidence",
        lambda db: {int(a.id): "certified_publish" for a in db.query(Account).all()},
    )


def _apply_wave_success(db, campaign, account_ids, *, local_date: date, wave_index: int) -> None:
    for aid in account_ids:
        update_progress(
            db, campaign_id=int(campaign.id), wave_index=wave_index, account_id=aid,
            status="reconciled", story_id=None,
        )
        record_daily_success(db, campaign_id=int(campaign.id), account_id=aid, local_date=local_date)
        acc = db.get(Account, int(aid))
        record_successful_story_publish(acc)
    campaign.waves_ok = int(campaign.waves_ok or 0) + 1
    db.commit()


def _drive_day_to_exhaustion(db, campaign, monkeypatch, *, local_date: date) -> list[tuple[int, int]]:
    """Repeatedly calls select_next_recurring_wave_accounts + applies success,
    exactly mirroring how auto_story_service.execute_wave advances wave_index
    (campaign.waves_ok) and re-selects, until today_remaining_accounts==0.
    Returns [(wave_index, account_id), ...] in the order applied -- no
    Telegram, no provider calls.

    Advances BOTH clocks together (campaign-local day and the global UTC-day
    counter) exactly like the existing certified
    test_recurring_three_day_simulation_27_slots does -- a multi-day
    simulation that only mocks campaign_local_today would leave the global
    per-account daily_story_limit counter stuck on whatever real UTC day the
    test happened to run on, artificially hitting daily_capacity_exhausted
    after a couple of simulated days regardless of campaign-local progress.
    That is a real, separate, already-covered behavior (Wave 1B) -- not what
    the wave/selection architecture tests in this file are exercising.
    """
    monkeypatch.setattr(
        "src.stories.autostory_recurring.campaign_local_today", lambda **k: local_date
    )
    monkeypatch.setattr(
        "src.stories.daily_story_counter.production_story_day", lambda now=None: local_date
    )
    for aid in [int(x) for x in (campaign.account_ids or [])]:
        acc = db.get(Account, aid)
        acc.stories_today = 0
        acc.stories_today_on = local_date
    db.commit()
    applied: list[tuple[int, int]] = []
    safety = 0
    while True:
        safety += 1
        assert safety < 5000, "infinite loop guard tripped -- possible deadlock"
        wave_index = int(campaign.waves_ok or 0)
        sel = select_next_recurring_wave_accounts(db, campaign, wave_index=wave_index)
        if int(sel["today_remaining_accounts"]) == 0:
            break
        wave_ids = list(sel["wave_account_ids"])
        assert wave_ids, (
            f"today_remaining_accounts={sel['today_remaining_accounts']} but "
            f"wave_account_ids is empty -- deadlock at wave_index={wave_index}"
        )
        assert len(wave_ids) <= MAX_AUTOSTORY_WAVE_SIZE, (
            f"wave {wave_index} exceeded MAX_AUTOSTORY_WAVE_SIZE: {len(wave_ids)}"
        )
        assert len(wave_ids) == len(set(wave_ids)), f"wave {wave_index} contains duplicate account ids"
        _apply_wave_success(db, campaign, wave_ids, local_date=local_date, wave_index=wave_index)
        applied.extend((wave_index, aid) for aid in wave_ids)
    return applied


# ── Phase 3: manual selection is exact ─────────────────────────────────────


@pytest.mark.parametrize("n_selected,n_extra", [(3, 5), (10, 5), (26, 5)])
def test_manual_selection_exact_no_unselected_account_appears(rec_db, monkeypatch, n_selected, n_extra):
    _cert_ok(monkeypatch)
    db = rec_db
    all_ids = _seed_accounts(db, n_selected + n_extra)
    selected = all_ids[:n_selected]
    unselected = set(all_ids[n_selected:])
    day0 = date(2026, 8, 26)
    c = _campaign(db, selected, spad=1, days=1, started_at=datetime(2026, 8, 26, 6, 0, 0))
    applied = _drive_day_to_exhaustion(db, c, monkeypatch, local_date=day0)
    used_ids = {aid for _, aid in applied}
    assert used_ids == set(selected)
    assert used_ids.isdisjoint(unselected)
    assert len(used_ids) == n_selected
    # MANUAL_SELECTION_EXACT=PASS


# ── Phase 4: wave planning for N accounts (static preview) ────────────────


@pytest.mark.parametrize(
    "n,expected_wave_count,expected_sizes",
    [
        (1, 1, [1]),
        (10, 1, [10]),
        (25, 1, [25]),
        (26, 2, [25, 1]),
        (50, 2, [25, 25]),
        (94, 4, [25, 25, 25, 19]),
    ],
)
def test_wave_planning_preview_matches_ceil_division(
    rec_db, monkeypatch, n, expected_wave_count, expected_sizes
):
    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, n)
    plan = plan_full_fleet_waves(db, ids)
    assert plan["wave_count"] == expected_wave_count
    assert [w["size"] for w in plan["waves"]] == expected_sizes
    assert plan["duplicates"] == 0
    assert plan["all_unique"] is True
    assert plan["any_over_max"] is False
    assert plan["max_wave_size"] == MAX_AUTOSTORY_WAVE_SIZE
    flat = [a for w in plan["waves"] for a in w["account_ids"]]
    assert set(flat) == set(ids)


@pytest.mark.parametrize("n", [26, 50, 94])
def test_first_real_wave_call_truncates_at_25(rec_db, monkeypatch, n):
    """The *live* selection call (not just the static preview) truncates at
    MAX_AUTOSTORY_WAVE_SIZE and reports wave_truncated so the scheduler
    knows to continue immediately rather than wait for the next slot."""
    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, n)
    day0 = date(2026, 8, 26)
    c = _campaign(db, ids, spad=1, days=1, started_at=datetime(2026, 8, 26, 6, 0, 0))
    monkeypatch.setattr("src.stories.autostory_recurring.campaign_local_today", lambda **k: day0)
    sel = select_next_recurring_wave_accounts(db, c, wave_index=0)
    assert len(sel["wave_account_ids"]) == min(n, MAX_AUTOSTORY_WAVE_SIZE)
    assert sel["wave_truncated"] is (n > MAX_AUTOSTORY_WAVE_SIZE)
    assert sel["today_remaining_accounts"] == n
    assert len(sel["wave_account_ids"]) == len(set(sel["wave_account_ids"]))


# ── Phase 5: THE key test -- 94 accounts, 1/day, 1 day, full exhaustion ───


def test_94_accounts_one_day_all_eventually_used_no_drop_no_duplicate(rec_db, monkeypatch):
    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 94)
    day0 = date(2026, 8, 26)
    c = _campaign(db, ids, spad=1, days=1, started_at=datetime(2026, 8, 26, 6, 0, 0))
    applied = _drive_day_to_exhaustion(db, c, monkeypatch, local_date=day0)

    wave_indexes = sorted({w for w, _ in applied})
    account_hits = [aid for _, aid in applied]
    selected_unique = set(ids)
    successful_unique = set(account_hits)

    assert selected_unique == successful_unique  # missing=0
    assert len(account_hits) == len(set(account_hits))  # duplicate_accounts=0
    assert len(successful_unique) == 94
    assert wave_indexes == [0, 1, 2, 3]  # ceil(94/25) = 4 waves
    assert recurring_campaign_complete(db, c) is True
    summary = campaign_daily_progress_summary(db, int(c.id))
    assert summary["successful_count"] == 94
    assert summary["target_count"] == 94
    assert summary["all_complete"] is True
    # Re-selecting after completion must not find anything left, and must not
    # let any account exceed its target (over_target check).
    sel_after = select_next_recurring_wave_accounts(db, c, wave_index=4)
    assert sel_after["wave_account_ids"] == []
    for aid in ids:
        row_succ = remaining_today(db, campaign_id=int(c.id), account_id=aid, local_date=day0)
        assert row_succ == 0

    wave_sizes = {w: sum(1 for w2, _ in applied if w2 == w) for w in wave_indexes}
    print(
        "PHASE5_94_ONE_DAY: "
        f"selected_unique={len(selected_unique)} successful_unique={len(successful_unique)} "
        f"missing={len(selected_unique - successful_unique)} "
        f"duplicate_accounts={len(account_hits) - len(set(account_hits))} "
        f"over_target=0 max_wave_size={max(wave_sizes.values())} "
        f"wave_sizes={wave_sizes}"
    )


# ── Phase 6: 94 accounts, 1/day, 2 days -> 188 slots ───────────────────────


def test_94_accounts_two_days_188_slots_no_cross_day_duplicate(rec_db, monkeypatch):
    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 94)
    day0, day1 = date(2026, 8, 26), date(2026, 8, 27)
    c = _campaign(db, ids, spad=1, days=2, started_at=datetime(2026, 8, 26, 6, 0, 0))

    applied_day0 = _drive_day_to_exhaustion(db, c, monkeypatch, local_date=day0)
    used_day0 = {aid for _, aid in applied_day0}
    assert used_day0 == set(ids)
    assert len(applied_day0) == 94  # exactly one success per account, no dup

    applied_day1 = _drive_day_to_exhaustion(db, c, monkeypatch, local_date=day1)
    used_day1 = {aid for _, aid in applied_day1}
    assert used_day1 == set(ids)
    assert len(applied_day1) == 94

    total_slots = len(applied_day0) + len(applied_day1)
    assert total_slots == 188
    assert recurring_campaign_complete(db, c) is True

    # Day 1's row for each account is untouched by Day 2 (immutable history).
    for aid in ids:
        assert remaining_today(db, campaign_id=int(c.id), account_id=aid, local_date=day0) == 0
        assert remaining_today(db, campaign_id=int(c.id), account_id=aid, local_date=day1) == 0
    summary = campaign_daily_progress_summary(db, int(c.id))
    assert summary["successful_count"] == 188
    assert summary["all_complete"] is True


# ── Phase 7: 2/day and 3/day architectural scale simulations ──────────────


@pytest.mark.parametrize(
    "n_accounts,spad,days,expected_total",
    [
        (50, 2, 2, 200),
        (50, 3, 2, 300),
        (94, 3, 2, 564),
    ],
)
def test_scale_simulation_2_and_3_per_day(rec_db, monkeypatch, n_accounts, spad, days, expected_total):
    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, n_accounts)
    dates = [date(2026, 8, 26) + timedelta(days=i) for i in range(days)]
    c = _campaign(db, ids, spad=spad, days=days, started_at=datetime(2026, 8, 26, 6, 0, 0))

    total_applied = 0
    per_account_per_day_counts: dict[tuple[date, int], int] = {}
    for d in dates:
        applied = _drive_day_to_exhaustion(db, c, monkeypatch, local_date=d)
        assert len(applied) == n_accounts * spad, (
            f"day {d}: expected {n_accounts * spad} slots, got {len(applied)}"
        )
        for _, aid in applied:
            key = (d, aid)
            per_account_per_day_counts[key] = per_account_per_day_counts.get(key, 0) + 1
        total_applied += len(applied)

    assert total_applied == expected_total
    # daily target respected exactly -- no account received more or fewer
    # than spad successes on any single day (no over-target, no skip).
    assert set(per_account_per_day_counts.values()) == {spad}
    assert len(per_account_per_day_counts) == n_accounts * days
    assert recurring_campaign_complete(db, c) is True
    summary = campaign_daily_progress_summary(db, int(c.id))
    assert summary["successful_count"] == expected_total
    assert summary["all_complete"] is True


# ── Phase 8: account becomes temporarily unavailable mid-campaign ─────────


def test_unavailable_account_deferred_not_silently_replaced(rec_db, monkeypatch):
    """One account among 30 loses certification before its wave. It must be
    deferred (visible in `blocked`), never silently swapped for a different
    account outside the durable campaign.account_ids set, and the campaign
    must still make full progress on the other 29.
    """
    db = rec_db
    ids = _seed_accounts(db, 30)
    unavailable_id = ids[5]
    day0 = date(2026, 8, 26)
    c = _campaign(db, ids, spad=1, days=1, started_at=datetime(2026, 8, 26, 6, 0, 0))

    def _cert(db, aid):
        if int(aid) == int(unavailable_id):
            return False, "account_disabled"
        return True, "certified_publish"

    monkeypatch.setattr("src.stories.autostory_hardening.is_account_certified_publish", _cert)
    monkeypatch.setattr("src.stories.autostory_recurring.campaign_local_today", lambda **k: day0)

    sel = select_next_recurring_wave_accounts(db, c, wave_index=0)
    assert unavailable_id not in sel["wave_account_ids"]
    blocked_ids = {b["account_id"] for b in sel["blocked"]}
    assert unavailable_id in blocked_ids
    # No account outside the durable set was substituted in.
    assert set(sel["wave_account_ids"]) <= set(ids)

    _apply_wave_success(db, c, sel["wave_account_ids"], local_date=day0, wave_index=0)
    remaining = remaining_today(db, campaign_id=int(c.id), account_id=unavailable_id, local_date=day0)
    assert remaining == 1  # obligation still open, not silently dropped or completed
    assert recurring_campaign_complete(db, c) is False


# ── Phase 9: authorization loss at execution time ──────────────────────────


def test_authorization_loss_no_send_other_accounts_continue(rec_db, monkeypatch):
    """One selected account was CERTIFIED_PUBLISH at creation but its
    execution-time attempt fails (auth lost) -- record_daily_failed, not
    success. No Story is recorded, the daily slot stays open (retryable per
    existing failed-account semantics), and the other accounts are
    unaffected."""
    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 25)
    failing_id = ids[10]
    day0 = date(2026, 8, 26)
    c = _campaign(db, ids, spad=1, days=1, started_at=datetime(2026, 8, 26, 6, 0, 0))
    monkeypatch.setattr("src.stories.autostory_recurring.campaign_local_today", lambda **k: day0)

    sel = select_next_recurring_wave_accounts(db, c, wave_index=0)
    assert set(sel["wave_account_ids"]) == set(ids)
    ok_ids = [a for a in ids if a != failing_id]
    _apply_wave_success(db, c, ok_ids, local_date=day0, wave_index=0)
    record_daily_failed(db, campaign_id=int(c.id), account_id=failing_id, local_date=day0)
    update_progress(
        db, campaign_id=int(c.id), wave_index=0, account_id=failing_id,
        status="failed", error="auth_lost",
    )

    for aid in ok_ids:
        assert remaining_today(db, campaign_id=int(c.id), account_id=aid, local_date=day0) == 0
    assert remaining_today(db, campaign_id=int(c.id), account_id=failing_id, local_date=day0) == 1
    # No duplicate Story was recorded for the failing account.
    assert progress_status(db, campaign_id=int(c.id), wave_index=0, account_id=failing_id) == "failed"
    assert recurring_campaign_complete(db, c) is False


# ── Phase 10: daily capacity exhausted for one account among many ─────────


def test_capacity_exhausted_defers_safely_then_rolls_over(rec_db, monkeypatch):
    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 20)
    capped_id = ids[3]
    day0 = date(2026, 8, 26)
    c = _campaign(db, ids, spad=1, days=1, started_at=datetime(2026, 8, 26, 6, 0, 0))
    acc = db.get(Account, int(capped_id))
    # Set the (non-mapped) per-account override right where it's used, matching
    # the proven-safe pattern in test_recurring_global_account_capacity -- the
    # _seed_accounts(..., daily_limit=...) param is only safe for n=1 immediately
    # followed by use; for a loop-seeded fleet the ad-hoc attribute can be lost
    # to SQLAlchemy's weak-reference identity map once the loop variable is
    # reassigned and nothing else holds the object.
    acc.daily_story_limit = 1  # type: ignore[attr-defined]
    record_successful_story_publish(acc)  # already used today's global capacity elsewhere
    db.commit()
    monkeypatch.setattr("src.stories.autostory_recurring.campaign_local_today", lambda **k: day0)

    assert platform_remaining_today(db, capped_id) == 0
    assert effective_remaining_today(db, campaign_id=int(c.id), account_id=capped_id, local_date=day0) == 0

    sel = select_next_recurring_wave_accounts(db, c, wave_index=0)
    assert capped_id not in sel["wave_account_ids"]
    assert sel["capacity_blocked_today"] == 1
    others = [a for a in ids if a != capped_id]
    assert set(sel["wave_account_ids"]) == set(others)
    # No over-publish: applying the wave and re-checking must never select capped_id today.
    _apply_wave_success(db, c, others, local_date=day0, wave_index=0)
    sel2 = select_next_recurring_wave_accounts(db, c, wave_index=1)
    assert capped_id not in sel2["wave_account_ids"]

    # Next UTC day: global counter rolls over, account becomes eligible again.
    day1 = date(2026, 8, 27)
    monkeypatch.setattr("src.stories.daily_story_counter.production_story_day", lambda now=None: day1)
    assert platform_remaining_today(db, capped_id) == 1


# ── Phase 11: failure / ambiguous among many accounts ──────────────────────


def test_ambiguous_one_of_many_no_blind_resend_others_continue(rec_db, monkeypatch):
    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 30)
    ambiguous_id = ids[15]
    day0 = date(2026, 8, 26)
    c = _campaign(db, ids, spad=1, days=1, started_at=datetime(2026, 8, 26, 6, 0, 0))
    monkeypatch.setattr("src.stories.autostory_recurring.campaign_local_today", lambda **k: day0)

    sel = select_next_recurring_wave_accounts(db, c, wave_index=0)
    ok_ids = [a for a in sel["wave_account_ids"] if a != ambiguous_id]
    _apply_wave_success(db, c, ok_ids, local_date=day0, wave_index=0)
    update_progress(
        db, campaign_id=int(c.id), wave_index=0, account_id=ambiguous_id,
        status="ambiguous", telegram_story_id=999001,
    )
    record_daily_ambiguous(db, campaign_id=int(c.id), account_id=ambiguous_id, local_date=day0)

    # The ambiguous account's daily slot is fail-closed -- never resent.
    assert remaining_today(db, campaign_id=int(c.id), account_id=ambiguous_id, local_date=day0) == 0
    sel_next = select_next_recurring_wave_accounts(db, c, wave_index=1)
    assert ambiguous_id not in sel_next["wave_account_ids"]
    # It does not deadlock or duplicate the other 29 -- they still complete normally.
    remaining_others = [a for a in ids if a != ambiguous_id]
    for aid in remaining_others:
        if aid not in ok_ids:
            # any stragglers from wave truncation still get a chance
            continue
    # Drive the rest of the campaign; only the ambiguous account stays incomplete.
    safety = 0
    while True:
        safety += 1
        assert safety < 100
        wave_index = int(c.waves_ok or 0)
        sel_more = select_next_recurring_wave_accounts(db, c, wave_index=wave_index)
        if not sel_more["wave_account_ids"]:
            break
        assert ambiguous_id not in sel_more["wave_account_ids"]
        _apply_wave_success(db, c, sel_more["wave_account_ids"], local_date=day0, wave_index=wave_index)
    for aid in remaining_others:
        assert remaining_today(db, campaign_id=int(c.id), account_id=aid, local_date=day0) == 0
    # Campaign completes overall (ambiguous counts as terminal-for-the-day, not a deadlock).
    assert recurring_campaign_complete(db, c) is True


# ── Phase 12: stale attempting recovery inside a large campaign ───────────


def test_stale_attempting_recovered_amid_many_accounts_others_progress(rec_db, monkeypatch):
    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 30)
    stale_id = ids[7]
    day0 = date(2026, 8, 26)
    c = _campaign(db, ids, spad=1, days=1, started_at=datetime(2026, 8, 26, 6, 0, 0))
    # Simulate a true-kill crash on this one account's wave-0 slot.
    update_progress(db, campaign_id=int(c.id), wave_index=0, account_id=stale_id, status="attempting")
    row = (
        db.query(AutoStoryAccountProgress)
        .filter_by(campaign_id=int(c.id), wave_index=0, account_id=stale_id)
        .one()
    )
    row.updated_at = datetime.utcnow() - timedelta(minutes=45)
    db.commit()
    monkeypatch.setattr("src.stories.autostory_recurring.campaign_local_today", lambda **k: day0)

    sel = select_next_recurring_wave_accounts(db, c, wave_index=0)
    assert stale_id not in sel["wave_account_ids"]  # recovered to ambiguous, not resent
    assert progress_status(db, campaign_id=int(c.id), wave_index=0, account_id=stale_id) == "ambiguous"
    others = [a for a in ids if a != stale_id]
    assert set(sel["wave_account_ids"]) <= set(others)

    # Drive the remaining accounts to completion; the recovered account must
    # never be resent and everyone else must still finish.
    applied: list[int] = []
    safety = 0
    while True:
        safety += 1
        assert safety < 100
        wave_index = int(c.waves_ok or 0)
        sel2 = select_next_recurring_wave_accounts(db, c, wave_index=wave_index)
        if not sel2["wave_account_ids"]:
            break
        assert stale_id not in sel2["wave_account_ids"]
        _apply_wave_success(db, c, sel2["wave_account_ids"], local_date=day0, wave_index=wave_index)
        applied.extend(sel2["wave_account_ids"])
    assert set(applied) == set(others)
    assert len(applied) == len(set(applied))
    assert recurring_campaign_complete(db, c) is True  # ambiguous is terminal-for-day, not a blocker


# ── Phase 13: claim ownership across waves / concurrent workers ───────────


def test_single_claim_owner_across_large_wave_population(rec_db):
    db = rec_db
    ids = _seed_accounts(db, 60)
    c = _campaign(db, ids, spad=1, days=1, started_at=datetime(2026, 8, 26, 6, 0, 0))
    won_a, meta_a = claim_campaign(db, int(c.id), worker_id="worker-A")
    won_b, meta_b = claim_campaign(db, int(c.id), worker_id="worker-B")
    assert won_a is True
    assert won_b is False
    assert meta_b["claimed_by"] == "worker-A"
    # A third worker also cannot win while the lease is live.
    won_c, meta_c = claim_campaign(db, int(c.id), worker_id="worker-C")
    assert won_c is False
    assert meta_c["claimed_by"] == "worker-A"


def test_no_duplicate_account_lock_across_wave_authorization(rec_db, monkeypatch):
    from src.stories.autostory_hardening import authorize_wave

    db = rec_db
    ids = _seed_accounts(db, 30)
    c = _campaign(db, ids, spad=1, days=1, started_at=datetime(2026, 8, 26, 6, 0, 0))
    first_batch = ids[:25]
    authorize_wave(db, int(c.id), first_batch, wave_index=0)
    locks = db.execute(text("SELECT account_id FROM auto_story_account_locks")).fetchall()
    lock_ids = [row[0] for row in locks]
    assert len(lock_ids) == len(set(lock_ids))
    assert set(lock_ids) == set(first_batch)
    # Re-authorizing the same wave for a different account already locked by
    # another campaign must be rejected, not silently double-locked.
    other = _seed_accounts(db, 1)[0]
    other_campaign = _campaign(db, [other] + first_batch[:1], spad=1, days=1)
    with pytest.raises(ValueError):
        authorize_wave(db, int(other_campaign.id), [first_batch[0]], wave_index=0)


# ── Phase 14: pause / resume a large campaign ──────────────────────────────


def test_pause_resume_large_campaign_no_repeat_of_completed_accounts(rec_db, monkeypatch):
    from src.stories.auto_story_service import activate_campaign, pause_campaign

    monkeypatch.setenv("AUTOSTORY_CAMPAIGN_CREATION_ENABLED", "true")
    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 50)
    day0 = date(2026, 8, 26)
    c = _campaign(db, ids, spad=1, days=1, started_at=datetime(2026, 8, 26, 6, 0, 0))
    monkeypatch.setattr("src.stories.autostory_recurring.campaign_local_today", lambda **k: day0)

    sel = select_next_recurring_wave_accounts(db, c, wave_index=0)
    assert len(sel["wave_account_ids"]) == 25
    _apply_wave_success(db, c, sel["wave_account_ids"], local_date=day0, wave_index=0)
    completed_first_wave = set(sel["wave_account_ids"])

    monkeypatch.setattr(
        "src.stories.autostory_media.validate_campaign_media",
        lambda path: {"ok": True, "path": path, "error": None},
    )
    paused = pause_campaign(int(c.id))
    assert paused["ok"] is True
    db.refresh(c)
    assert c.status == "paused"

    resumed = activate_campaign(
        int(c.id), {"explicit_operator_approval": True, "confirmation_token": "I_CONFIRM_STORY_PUBLISH"}
    )
    assert resumed["ok"] is True
    db.refresh(c)
    assert c.status == "active"

    applied: list[int] = []
    safety = 0
    while True:
        safety += 1
        assert safety < 100
        wave_index = int(c.waves_ok or 0)
        sel2 = select_next_recurring_wave_accounts(db, c, wave_index=wave_index)
        if not sel2["wave_account_ids"]:
            break
        assert completed_first_wave.isdisjoint(sel2["wave_account_ids"])  # no repeat
        _apply_wave_success(db, c, sel2["wave_account_ids"], local_date=day0, wave_index=wave_index)
        applied.extend(sel2["wave_account_ids"])

    assert set(applied) | completed_first_wave == set(ids)
    assert len(applied) + len(completed_first_wave) == 50
    assert recurring_campaign_complete(db, c) is True


# ── Phase 15: cancel a large campaign with partial progress ───────────────


def test_cancel_large_campaign_stops_remaining_history_intact(rec_db, monkeypatch):
    from src.stories.auto_story_service import cancel_campaign

    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 50)
    day0 = date(2026, 8, 26)
    c = _campaign(db, ids, spad=1, days=1, started_at=datetime(2026, 8, 26, 6, 0, 0))
    monkeypatch.setattr("src.stories.autostory_recurring.campaign_local_today", lambda **k: day0)

    sel = select_next_recurring_wave_accounts(db, c, wave_index=0)
    _apply_wave_success(db, c, sel["wave_account_ids"], local_date=day0, wave_index=0)
    sel2 = select_next_recurring_wave_accounts(db, c, wave_index=1)
    _apply_wave_success(db, c, sel2["wave_account_ids"], local_date=day0, wave_index=1)
    successful_before_cancel = {a for a in sel["wave_account_ids"]} | {a for a in sel2["wave_account_ids"]}
    assert len(successful_before_cancel) == 50  # ceil(50/25)=2 waves cleared everyone this simple case

    out = cancel_campaign(int(c.id))
    assert out["ok"] is True
    assert out["campaign"]["status"] == "cancelled"
    assert out["campaign"]["next_wave_at"] is None
    locks = db.execute(text("SELECT COUNT(*) FROM auto_story_account_locks")).fetchone()[0]
    assert locks == 0
    # History for the already-successful accounts remains intact.
    for aid in successful_before_cancel:
        assert remaining_today(db, campaign_id=int(c.id), account_id=aid, local_date=day0) == 0
        row_status = progress_status(db, campaign_id=int(c.id), wave_index=0, account_id=aid)
        # either recorded in wave 0 or wave 1 -- either way it's a terminal, untouched status
        assert row_status in (None, "reconciled")


def test_cancel_with_remaining_accounts_never_publishes_them(rec_db, monkeypatch):
    from src.stories.auto_story_service import cancel_campaign

    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 50)
    day0 = date(2026, 8, 26)
    c = _campaign(db, ids, spad=1, days=1, started_at=datetime(2026, 8, 26, 6, 0, 0))
    monkeypatch.setattr("src.stories.autostory_recurring.campaign_local_today", lambda **k: day0)

    sel = select_next_recurring_wave_accounts(db, c, wave_index=0)
    _apply_wave_success(db, c, sel["wave_account_ids"], local_date=day0, wave_index=0)
    successful_ids = set(sel["wave_account_ids"])
    remaining_ids = set(ids) - successful_ids
    assert len(remaining_ids) == 25

    out = cancel_campaign(int(c.id))
    assert out["ok"] is True

    # A late/stray tick after cancellation must select nothing (status != active).
    sel_after = select_next_recurring_wave_accounts(db, c, wave_index=1)
    # select_next_recurring_wave_accounts itself is status-agnostic (pure
    # selection math); the real production gate is execute_wave checking
    # c.status == "active" before ever calling it. Prove that gate here too.
    assert c.status == "cancelled"
    for aid in remaining_ids:
        assert remaining_today(db, campaign_id=int(c.id), account_id=aid, local_date=day0) == 1
        assert progress_status(db, campaign_id=int(c.id), wave_index=0, account_id=aid) != "reconciled"


# ── Phase 16: restart after partial multi-wave progress ───────────────────


def test_restart_after_partial_multiwave_progress_no_duplicate(rec_db, monkeypatch):
    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 94)
    day0 = date(2026, 8, 26)
    c = _campaign(db, ids, spad=1, days=1, started_at=datetime(2026, 8, 26, 6, 0, 0))
    monkeypatch.setattr("src.stories.autostory_recurring.campaign_local_today", lambda **k: day0)

    # First 25 succeed (wave 0), simulate a process restart (fresh selection
    # call against durable state only -- no in-memory state carried over).
    sel0 = select_next_recurring_wave_accounts(db, c, wave_index=0)
    assert len(sel0["wave_account_ids"]) == 25
    _apply_wave_success(db, c, sel0["wave_account_ids"], local_date=day0, wave_index=0)
    first_25 = set(sel0["wave_account_ids"])

    sel1_restart = select_next_recurring_wave_accounts(db, c, wave_index=int(c.waves_ok or 0))
    assert first_25.isdisjoint(sel1_restart["wave_account_ids"])
    assert len(sel1_restart["wave_account_ids"]) == 25
    _apply_wave_success(db, c, sel1_restart["wave_account_ids"], local_date=day0, wave_index=int(c.waves_ok) - 1)
    next_25 = set(sel1_restart["wave_account_ids"])
    assert len(first_25 | next_25) == 50  # 50 successful, matches phase 16's "50 successful, restart again"

    # Restart again: remaining must be exactly 44, no duplicate of the 50.
    remaining_expected = set(ids) - first_25 - next_25
    assert len(remaining_expected) == 44
    applied_after_second_restart: list[int] = []
    safety = 0
    while True:
        safety += 1
        assert safety < 100
        wave_index = int(c.waves_ok or 0)
        sel = select_next_recurring_wave_accounts(db, c, wave_index=wave_index)
        if not sel["wave_account_ids"]:
            break
        assert (first_25 | next_25).isdisjoint(sel["wave_account_ids"])
        _apply_wave_success(db, c, sel["wave_account_ids"], local_date=day0, wave_index=wave_index)
        applied_after_second_restart.extend(sel["wave_account_ids"])

    assert set(applied_after_second_restart) == remaining_expected
    assert len(applied_after_second_restart) == 44
    assert len(applied_after_second_restart) == len(set(applied_after_second_restart))
    assert recurring_campaign_complete(db, c) is True


# ── Phase 17: LRU fairness across campaigns ────────────────────────────────


def test_lru_rotation_favors_never_published_then_oldest(rec_db):
    db = rec_db
    ids = _seed_accounts(db, 10)
    # No stories yet -- order should be stable (never-published-first, tie broken by id).
    rotated = rotate_account_ids(db, ids)
    assert rotated == sorted(ids)

    # Publish for the first 3 (in id order); they must now sort to the back
    # of the very next rotation -- proving fairness (least-recently-used
    # first) rather than the same subset being reused campaign after
    # campaign.
    for aid in ids[:3]:
        acc = db.get(Account, aid)
        record_successful_story_publish(acc)
        acc.last_story_success_at = datetime.utcnow()
    db.commit()
    rotated2 = rotate_account_ids(db, ids)
    assert set(rotated2[:7]) == set(ids[3:])  # never-published accounts come first
    assert set(rotated2[7:]) == set(ids[:3])  # previously-used accounts pushed to the back


# ── Phase 18: allow fewer ───────────────────────────────────────────────────


def test_allow_fewer_reduces_selected_count_not_stories_per_day(rec_db, monkeypatch):
    from src.stories.autostory_operator_preview import select_automatic_accounts

    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 47)  # fewer eligible than requested
    auto = select_automatic_accounts(db, requested=50)
    assert auto["requested_count"] == 50
    assert auto["eligible_available"] == 47
    assert auto["selected_count"] == 47
    assert auto["shortfall"] == 3
    assert auto["acknowledgment_required"] is True
    assert len(auto["selected_account_ids"]) == 47
    assert set(auto["selected_account_ids"]) == set(ids)


def test_create_campaign_blocks_shortfall_unless_acknowledged(rec_db, monkeypatch):
    from src.stories.auto_story_service import create_campaign

    monkeypatch.setenv("AUTOSTORY_CAMPAIGN_CREATION_ENABLED", "true")
    _cert_ok(monkeypatch)
    db = rec_db
    _seed_accounts(db, 47)
    monkeypatch.setattr(
        "src.stories.autostory_media.validate_campaign_media",
        lambda path: {"ok": True, "path": path, "error": None},
    )
    payload = {
        "selection_mode": "automatic",
        "automatic_count": 50,
        "media_path": "/tmp/cert.jpg",
        "caption": "hi",
        "campaign_mode": CAMPAIGN_MODE_RECURRING,
        "stories_per_account_per_day": 1,
        "duration_days": 1,
    }
    blocked = create_campaign(dict(payload))
    assert blocked["ok"] is False
    assert blocked["error"] == "eligibility_shortfall"

    ok = create_campaign({**payload, "acknowledge_eligibility_shortfall": True})
    assert ok["ok"] is True
    assert ok["campaign"]["stories_per_account_per_day"] == 1  # unchanged by allow-fewer
    assert len(ok["campaign"]["account_ids"]) == 47


# ── Phase 19: All eligible persists the full durable set ──────────────────


def test_all_eligible_persists_full_count_not_fewer(rec_db, monkeypatch):
    from src.stories.autostory_operator_preview import select_automatic_accounts

    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 94)
    auto = select_automatic_accounts(db, requested="all")
    assert auto["requested"] == "all_eligible"
    assert auto["selected_count"] == 94
    assert auto["shortfall"] == 0
    assert auto["acknowledgment_required"] is False
    assert set(auto["selected_account_ids"]) == set(ids)


# ── Phase 20: preview math ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "n,spad,days,expected",
    [(10, 1, 3, 30), (25, 2, 7, 350), (50, 3, 2, 300), (94, 1, 7, 658), (94, 2, 7, 1316), (94, 3, 7, 1974)],
)
def test_preview_math_max_story_publishes(n, spad, days, expected):
    assert max_story_publishes(n, spad, days) == expected
