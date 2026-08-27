"""Local certification of recurring_daily AutoStory (no Telegram, no production).

Invariants A–J plus multi-story/day, capacity, pause/resume/cancel, and
crash/retry semantics. Publication is simulated via durable progress rows.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.core.database import Base
from src.core.models import (
    Account,
    AccountStatus,
    AutoStoryAccountProgress,
    AutoStoryCampaign,
    AutoStoryDailyProgress,
)
from src.stories.autostory_hardening import (
    PROGRESS_NO_RESEND,
    claim_campaign,
    progress_status,
    update_progress,
)
from src.stories.autostory_recurring import (
    CAMPAIGN_MODE_ONCE,
    CAMPAIGN_MODE_RECURRING,
    CERTIFIED_MAX_STORIES_PER_ACCOUNT_PER_DAY,
    RECURRING_PROGRESS_STATUS_MATRIX,
    campaign_local_dates,
    campaign_local_today,
    clamp_stories_per_account_per_day,
    effective_remaining_today,
    ensure_daily_progress_rows,
    get_daily_row,
    next_monotonic_wave_index,
    plan_recurring_dry_run_calendar,
    platform_remaining_today,
    record_daily_ambiguous,
    record_daily_failed,
    record_daily_success,
    reconcile_daily_from_wave_slot,
    remaining_today,
    recurring_campaign_complete,
    recurring_campaign_ends_at,
    select_next_recurring_wave_accounts,
    spread_local_times,
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
    monkeypatch.setattr("src.stories.auto_story_service.get_db_context", _ctx)
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


def _seed_accounts(db, n: int, *, daily_limit: int | None = None) -> list[int]:
    ids = []
    for i in range(n):
        a = Account(
            phone_number=f"+1555100{i:04d}",
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
    days: int = 3,
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
        db,
        campaign_id=int(c.id),
        account_ids=ids,
        local_dates=dates,
        target_count=spad,
    )
    db.commit()
    return c


def _cert_ok(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.stories.autostory_hardening.is_account_certified_publish",
        lambda db, aid: (True, "certified_publish"),
    )


def _apply_wave_success(db, campaign, account_ids, *, local_date: date, wave_index: int) -> None:
    for aid in account_ids:
        update_progress(
            db,
            campaign_id=int(campaign.id),
            wave_index=wave_index,
            account_id=aid,
            status="reconciled",
            story_id=None,
        )
        record_daily_success(
            db,
            campaign_id=int(campaign.id),
            account_id=aid,
            local_date=local_date,
        )
        acc = db.get(Account, int(aid))
        record_successful_story_publish(acc)
    campaign.waves_ok = int(campaign.waves_ok or 0) + 1
    db.commit()


def test_status_matrix_matches_progress_no_resend():
    for status, spec in RECURRING_PROGRESS_STATUS_MATRIX.items():
        if status in PROGRESS_NO_RESEND:
            assert spec["retry"] is False
            assert spec["daily_target_consumed"] is True
    assert RECURRING_PROGRESS_STATUS_MATRIX["failed"]["retry"] is True
    assert RECURRING_PROGRESS_STATUS_MATRIX["failed"]["daily_target_consumed"] is False
    assert RECURRING_PROGRESS_STATUS_MATRIX["ambiguous"]["retry"] is False
    assert RECURRING_PROGRESS_STATUS_MATRIX["claimed"]["retry"] is True


def test_certified_range_is_1_to_3(monkeypatch):
    monkeypatch.setenv("MAX_STORIES_PER_ACCOUNT_PER_DAY", "3")
    assert CERTIFIED_MAX_STORIES_PER_ACCOUNT_PER_DAY == 3
    assert clamp_stories_per_account_per_day(1) == 1
    assert clamp_stories_per_account_per_day(2) == 2
    assert clamp_stories_per_account_per_day(3) == 3
    assert clamp_stories_per_account_per_day(9) == 3


def test_window_spread_three_stories_not_back_to_back():
    times = spread_local_times(start_hhmm="10:00", end_hhmm="20:00", count=3)
    assert times == ["10:00", "15:00", "20:00"]


def test_recurring_day_one_executes_target(rec_db, monkeypatch):
    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 2)
    day0 = date(2026, 8, 26)
    c = _campaign(db, ids, spad=1, days=2, started_at=datetime(2026, 8, 26, 6, 0, 0))
    monkeypatch.setattr(
        "src.stories.autostory_recurring.campaign_local_today", lambda **k: day0
    )
    sel = select_next_recurring_wave_accounts(db, c, wave_index=0)
    assert set(sel["wave_account_ids"]) == set(ids)
    _apply_wave_success(db, c, ids, local_date=day0, wave_index=0)
    for aid in ids:
        assert remaining_today(db, campaign_id=int(c.id), account_id=aid, local_date=day0) == 0
    sel2 = select_next_recurring_wave_accounts(db, c, wave_index=1)
    assert sel2["wave_account_ids"] == []


def test_recurring_next_day_resets_target(rec_db, monkeypatch):
    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 1)
    day0, day1 = date(2026, 8, 26), date(2026, 8, 27)
    c = _campaign(db, ids, spad=1, days=2, started_at=datetime(2026, 8, 26, 6, 0, 0))
    record_daily_success(db, campaign_id=int(c.id), account_id=ids[0], local_date=day0)
    assert remaining_today(db, campaign_id=int(c.id), account_id=ids[0], local_date=day0) == 0
    assert remaining_today(db, campaign_id=int(c.id), account_id=ids[0], local_date=day1) == 1
    monkeypatch.setattr(
        "src.stories.autostory_recurring.campaign_local_today", lambda **k: day1
    )
    sel = select_next_recurring_wave_accounts(db, c, wave_index=1)
    assert ids[0] in sel["wave_account_ids"]


def test_recurring_three_stories_per_day(rec_db, monkeypatch):
    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 1)
    day0 = date(2026, 8, 26)
    c = _campaign(db, ids, spad=3, days=1, started_at=datetime(2026, 8, 26, 6, 0, 0))
    monkeypatch.setattr(
        "src.stories.autostory_recurring.campaign_local_today", lambda **k: day0
    )
    for slot in range(3):
        assert remaining_today(db, campaign_id=int(c.id), account_id=ids[0], local_date=day0) == 3 - slot
        sel = select_next_recurring_wave_accounts(db, c, wave_index=slot)
        assert sel["wave_account_ids"] == ids
        assert sel["wave_truncated"] is False
        _apply_wave_success(db, c, ids, local_date=day0, wave_index=slot)
    assert remaining_today(db, campaign_id=int(c.id), account_id=ids[0], local_date=day0) == 0
    sel_done = select_next_recurring_wave_accounts(db, c, wave_index=3)
    assert sel_done["wave_account_ids"] == []


def test_recurring_partial_day_continues(rec_db, monkeypatch):
    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 1)
    day0 = date(2026, 8, 26)
    c = _campaign(db, ids, spad=3, days=1, started_at=datetime(2026, 8, 26, 6, 0, 0))
    record_daily_success(db, campaign_id=int(c.id), account_id=ids[0], local_date=day0)
    assert remaining_today(db, campaign_id=int(c.id), account_id=ids[0], local_date=day0) == 2
    monkeypatch.setattr(
        "src.stories.autostory_recurring.campaign_local_today", lambda **k: day0
    )
    sel = select_next_recurring_wave_accounts(db, c, wave_index=1)
    assert ids[0] in sel["wave_account_ids"]


def test_recurring_completed_day_not_reselected(rec_db, monkeypatch):
    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 1)
    day0 = date(2026, 8, 26)
    c = _campaign(db, ids, spad=3, days=2, started_at=datetime(2026, 8, 26, 6, 0, 0))
    for _ in range(3):
        record_daily_success(db, campaign_id=int(c.id), account_id=ids[0], local_date=day0)
    monkeypatch.setattr(
        "src.stories.autostory_recurring.campaign_local_today", lambda **k: day0
    )
    sel = select_next_recurring_wave_accounts(db, c, wave_index=3)
    assert ids[0] not in sel["wave_account_ids"]
    assert remaining_today(db, campaign_id=int(c.id), account_id=ids[0], local_date=day0) == 0


def test_recurring_once_progress_does_not_block_next_day(rec_db, monkeypatch):
    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 1)
    day0, day1 = date(2026, 8, 26), date(2026, 8, 27)
    c = _campaign(db, ids, spad=1, days=2, started_at=datetime(2026, 8, 26, 6, 0, 0))
    db.add(
        AutoStoryAccountProgress(
            campaign_id=int(c.id),
            wave_index=0,
            account_id=ids[0],
            status="published",
        )
    )
    db.commit()
    record_daily_success(db, campaign_id=int(c.id), account_id=ids[0], local_date=day0)
    monkeypatch.setattr(
        "src.stories.autostory_recurring.campaign_local_today", lambda **k: day1
    )
    sel = select_next_recurring_wave_accounts(db, c, wave_index=1)
    assert ids[0] in sel["wave_account_ids"]


def test_recurring_no_duplicate_scheduler_tick(rec_db):
    db = rec_db
    ids = _seed_accounts(db, 1)
    c = _campaign(db, ids, spad=1, days=1)
    w1, _ = claim_campaign(db, int(c.id), worker_id="tick-1")
    w2, meta = claim_campaign(db, int(c.id), worker_id="tick-2")
    assert w1 is True
    assert w2 is False
    assert meta["claimed_by"] == "tick-1"


def test_recurring_monotonic_wave_indexes(rec_db, monkeypatch):
    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 1)
    day0, day1 = date(2026, 8, 26), date(2026, 8, 27)
    c = _campaign(db, ids, spad=3, days=2, started_at=datetime(2026, 8, 26, 6, 0, 0))
    monkeypatch.setattr(
        "src.stories.autostory_recurring.campaign_local_today", lambda **k: day0
    )
    seen: list[tuple[int, int]] = []
    for slot in range(3):
        wave = next_monotonic_wave_index(c)
        sel = select_next_recurring_wave_accounts(db, c, wave_index=wave)
        assert sel["wave_index"] == wave
        _apply_wave_success(db, c, sel["wave_account_ids"], local_date=day0, wave_index=wave)
        seen.append((wave, ids[0]))
    monkeypatch.setattr(
        "src.stories.autostory_recurring.campaign_local_today", lambda **k: day1
    )
    monkeypatch.setattr(
        "src.stories.daily_story_counter.production_story_day",
        lambda now=None: day1,
    )
    for aid in ids:
        acc = db.get(Account, aid)
        acc.stories_today = 0
        acc.stories_today_on = day1
    db.commit()
    wave_next = next_monotonic_wave_index(c)
    assert wave_next == 3
    sel = select_next_recurring_wave_accounts(db, c, wave_index=wave_next)
    assert sel["wave_index"] == 3
    keys = {(int(c.id), w, a) for w, a in seen + [(wave_next, ids[0])]}
    assert len(keys) == 4


def test_recurring_pause_resume_same_day(rec_db, monkeypatch):
    from src.stories.auto_story_service import activate_campaign, pause_campaign

    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 1)
    day0 = date(2026, 8, 26)
    c = _campaign(db, ids, spad=3, days=1, started_at=datetime(2026, 8, 26, 6, 0, 0))
    record_daily_success(db, campaign_id=int(c.id), account_id=ids[0], local_date=day0)
    monkeypatch.setattr(
        "src.stories.autostory_media.validate_campaign_media",
        lambda path: {"ok": True, "path": path, "error": None},
    )
    paused = pause_campaign(int(c.id))
    assert paused["ok"] is True
    assert paused["campaign"]["status"] == "paused"
    db.refresh(c)
    assert c.status == "paused"
    monkeypatch.setattr(
        "src.stories.autostory_recurring.campaign_local_today", lambda **k: day0
    )
    sel_paused = select_next_recurring_wave_accounts(db, c, wave_index=1)
    # Domain selection still sees remaining work; scheduler tick ignores paused.
    assert remaining_today(db, campaign_id=int(c.id), account_id=ids[0], local_date=day0) == 2
    resumed = activate_campaign(
        int(c.id),
        {"explicit_operator_approval": True, "confirmation_token": "I_CONFIRM_STORY_PUBLISH"},
    )
    assert resumed["ok"] is True
    assert resumed["campaign"]["status"] == "active"
    sel = select_next_recurring_wave_accounts(db, c, wave_index=1)
    assert ids[0] in sel["wave_account_ids"]


def test_recurring_pause_resume_next_day(rec_db, monkeypatch):
    from src.stories.auto_story_service import activate_campaign, pause_campaign

    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 1)
    day0, day1 = date(2026, 8, 26), date(2026, 8, 27)
    c = _campaign(db, ids, spad=1, days=2, started_at=datetime(2026, 8, 26, 6, 0, 0))
    record_daily_success(db, campaign_id=int(c.id), account_id=ids[0], local_date=day0)
    monkeypatch.setattr(
        "src.stories.autostory_media.validate_campaign_media",
        lambda path: {"ok": True, "path": path, "error": None},
    )
    assert pause_campaign(int(c.id))["ok"] is True
    resumed = activate_campaign(
        int(c.id),
        {"explicit_operator_approval": True, "confirmation_token": "I_CONFIRM_STORY_PUBLISH"},
    )
    assert resumed["ok"] is True
    monkeypatch.setattr(
        "src.stories.autostory_recurring.campaign_local_today", lambda **k: day1
    )
    sel = select_next_recurring_wave_accounts(db, c, wave_index=1)
    assert ids[0] in sel["wave_account_ids"]
    assert remaining_today(db, campaign_id=int(c.id), account_id=ids[0], local_date=day1) == 1


def test_recurring_cancel_stops_future_days(rec_db, monkeypatch):
    from src.stories.auto_story_service import cancel_campaign, tick_due_auto_story_campaigns

    db = rec_db
    ids = _seed_accounts(db, 1)
    c = _campaign(db, ids, spad=1, days=3, started_at=datetime(2026, 8, 26, 6, 0, 0))
    c.next_wave_at = datetime.utcnow() - timedelta(minutes=1)
    db.commit()
    out = cancel_campaign(int(c.id))
    assert out["ok"] is True
    assert out["campaign"]["status"] == "cancelled"
    assert out["campaign"]["next_wave_at"] is None
    fired = tick_due_auto_story_campaigns(now=datetime.utcnow())
    assert fired.get("skipped") or fired.get("due_ids") == [] or fired.get("fired") == []


def test_recurring_end_date_completes_campaign(rec_db, monkeypatch):
    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 1)
    start = datetime(2026, 8, 26, 6, 0, 0)
    c = _campaign(db, ids, spad=1, days=3, started_at=start)
    dates = campaign_local_dates(started_at=start, duration_days=3, tz_name="UTC")
    assert dates == [date(2026, 8, 26), date(2026, 8, 27), date(2026, 8, 28)]
    for d in dates:
        record_daily_success(db, campaign_id=int(c.id), account_id=ids[0], local_date=d)
    day4 = date(2026, 8, 29)
    monkeypatch.setattr(
        "src.stories.autostory_recurring.campaign_local_today", lambda **k: day4
    )
    sel = select_next_recurring_wave_accounts(db, c, wave_index=3)
    assert sel["wave_account_ids"] == []
    assert sel["unfinished_count"] == 0
    assert recurring_campaign_complete(db, c) is True
    assert remaining_today(db, campaign_id=int(c.id), account_id=ids[0], local_date=day4) == 0


def test_recurring_global_account_capacity(rec_db, monkeypatch):
    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 1)
    acc = db.get(Account, ids[0])
    acc.daily_story_limit = 1  # type: ignore[attr-defined]
    db.commit()
    day0 = date(2026, 8, 26)
    c = _campaign(db, ids, spad=3, days=1, started_at=datetime(2026, 8, 26, 6, 0, 0))
    record_successful_story_publish(acc)
    db.commit()
    monkeypatch.setattr(
        "src.stories.autostory_recurring.campaign_local_today", lambda **k: day0
    )
    assert remaining_today(db, campaign_id=int(c.id), account_id=ids[0], local_date=day0) == 3
    assert platform_remaining_today(db, ids[0]) == 0
    assert effective_remaining_today(
        db, campaign_id=int(c.id), account_id=ids[0], local_date=day0
    ) == 0
    sel = select_next_recurring_wave_accounts(db, c, wave_index=0)
    assert ids[0] not in sel["wave_account_ids"]
    assert sel["capacity_blocked_today"] == 1


def test_recurring_two_campaigns_share_global_capacity(rec_db, monkeypatch):
    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 1)
    acc = db.get(Account, ids[0])
    acc.daily_story_limit = 3  # type: ignore[attr-defined]
    db.commit()
    day0 = date(2026, 8, 26)
    started = datetime(2026, 8, 26, 6, 0, 0)
    a = _campaign(db, ids, spad=2, days=1, started_at=started)
    b = _campaign(db, ids, spad=2, days=1, started_at=started)
    monkeypatch.setattr(
        "src.stories.autostory_recurring.campaign_local_today", lambda **k: day0
    )
    for slot in range(2):
        sel = select_next_recurring_wave_accounts(db, a, wave_index=slot)
        assert ids[0] in sel["wave_account_ids"]
        _apply_wave_success(db, a, ids, local_date=day0, wave_index=slot)
    assert platform_remaining_today(db, ids[0]) == 1
    sel_b = select_next_recurring_wave_accounts(db, b, wave_index=0)
    assert ids[0] in sel_b["wave_account_ids"]
    _apply_wave_success(db, b, ids, local_date=day0, wave_index=0)
    assert platform_remaining_today(db, ids[0]) == 0
    sel_b2 = select_next_recurring_wave_accounts(db, b, wave_index=1)
    assert ids[0] not in sel_b2["wave_account_ids"]
    assert db.get(Account, ids[0]).stories_today == 3


def test_recurring_failed_account_does_not_block_healthy_accounts(rec_db, monkeypatch):
    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 3)
    day0 = date(2026, 8, 26)
    c = _campaign(db, ids, spad=1, days=1, started_at=datetime(2026, 8, 26, 6, 0, 0))
    record_daily_failed(db, campaign_id=int(c.id), account_id=ids[0], local_date=day0)
    monkeypatch.setattr(
        "src.stories.autostory_recurring.campaign_local_today", lambda **k: day0
    )
    sel = select_next_recurring_wave_accounts(db, c, wave_index=0)
    assert ids[0] in sel["wave_account_ids"]
    assert ids[1] in sel["wave_account_ids"]
    assert ids[2] in sel["wave_account_ids"]
    _apply_wave_success(db, c, ids[1:], local_date=day0, wave_index=0)
    assert remaining_today(db, campaign_id=int(c.id), account_id=ids[1], local_date=day0) == 0
    assert remaining_today(db, campaign_id=int(c.id), account_id=ids[0], local_date=day0) == 1


def test_recurring_ambiguous_does_not_blind_resend(rec_db, monkeypatch):
    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 1)
    day0 = date(2026, 8, 26)
    c = _campaign(db, ids, spad=3, days=1, started_at=datetime(2026, 8, 26, 6, 0, 0))
    record_daily_ambiguous(db, campaign_id=int(c.id), account_id=ids[0], local_date=day0)
    monkeypatch.setattr(
        "src.stories.autostory_recurring.campaign_local_today", lambda **k: day0
    )
    assert remaining_today(db, campaign_id=int(c.id), account_id=ids[0], local_date=day0) == 0
    sel = select_next_recurring_wave_accounts(db, c, wave_index=1)
    assert ids[0] not in sel["wave_account_ids"]
    r = record_daily_success(db, campaign_id=int(c.id), account_id=ids[0], local_date=day0)
    assert r["ok"] is False


def test_recurring_manual_accounts_not_replaced(rec_db, monkeypatch):
    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 4)
    extra = ids[3]
    chosen = ids[:3]
    day0 = date(2026, 8, 26)
    c = _campaign(db, chosen, spad=1, days=1, started_at=datetime(2026, 8, 26, 6, 0, 0))
    monkeypatch.setattr(
        "src.stories.autostory_recurring.campaign_local_today", lambda **k: day0
    )
    sel = select_next_recurring_wave_accounts(db, c, wave_index=0)
    assert extra not in sel["wave_account_ids"]
    assert set(sel["wave_account_ids"]) <= set(chosen)


def test_recurring_automatic_accounts_use_frozen_campaign_ids(rec_db, monkeypatch):
    from src.stories.autostory_operator_preview import select_automatic_accounts

    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 5)
    auto = select_automatic_accounts(db, requested=3)
    selected = list(auto.get("selected_account_ids") or ids[:3])
    if not selected:
        selected = ids[:3]
    day0 = date(2026, 8, 26)
    c = _campaign(db, selected, spad=1, days=2, started_at=datetime(2026, 8, 26, 6, 0, 0))
    disabled = db.get(Account, selected[0])
    disabled.status = AccountStatus.BANNED
    db.commit()

    def _cert(db, aid):
        if int(aid) == int(selected[0]):
            return False, "account_disabled"
        return True, "certified_publish"

    monkeypatch.setattr(
        "src.stories.autostory_hardening.is_account_certified_publish", _cert
    )
    monkeypatch.setattr(
        "src.stories.autostory_recurring.campaign_local_today", lambda **k: day0
    )
    sel = select_next_recurring_wave_accounts(db, c, wave_index=0)
    assert selected[0] not in sel["wave_account_ids"]
    healthy = [a for a in selected[1:] if a in sel["wave_account_ids"]]
    assert healthy
    outsiders = set(ids) - set(selected)
    assert outsiders.isdisjoint(sel["wave_account_ids"])


def test_recurring_midnight_boundary(monkeypatch):
    yerevan_eve = datetime(2026, 8, 26, 19, 59, 59, tzinfo=timezone.utc)
    yerevan_next = datetime(2026, 8, 26, 20, 0, 0, tzinfo=timezone.utc)
    assert campaign_local_today(now=yerevan_eve, tz_name="Asia/Yerevan") == date(2026, 8, 26)
    assert campaign_local_today(now=yerevan_next, tz_name="Asia/Yerevan") == date(2026, 8, 27)
    utc_eve = datetime(2026, 8, 26, 23, 59, 59, tzinfo=timezone.utc)
    utc_next = datetime(2026, 8, 27, 0, 0, 0, tzinfo=timezone.utc)
    assert campaign_local_today(now=utc_eve, tz_name="UTC") == date(2026, 8, 26)
    assert campaign_local_today(now=utc_next, tz_name="UTC") == date(2026, 8, 27)


def test_recurring_crash_after_claim_retries_same_slot_not_new_slot(rec_db, monkeypatch):
    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 1)
    day0 = date(2026, 8, 26)
    c = _campaign(db, ids, spad=2, days=1, started_at=datetime(2026, 8, 26, 6, 0, 0))
    update_progress(
        db,
        campaign_id=int(c.id),
        wave_index=0,
        account_id=ids[0],
        status="attempting",
    )
    monkeypatch.setattr(
        "src.stories.autostory_recurring.campaign_local_today", lambda **k: day0
    )
    assert remaining_today(db, campaign_id=int(c.id), account_id=ids[0], local_date=day0) == 2
    sel = select_next_recurring_wave_accounts(db, c, wave_index=0)
    assert ids[0] in sel["wave_account_ids"]
    assert next_monotonic_wave_index(c) == 0


def test_recurring_crash_after_reconcile_does_not_blind_resend(rec_db, monkeypatch):
    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 1)
    day0 = date(2026, 8, 26)
    c = _campaign(db, ids, spad=1, days=1, started_at=datetime(2026, 8, 26, 6, 0, 0))
    update_progress(
        db,
        campaign_id=int(c.id),
        wave_index=0,
        account_id=ids[0],
        status="reconciled",
        story_id=9001,
    )
    assert remaining_today(db, campaign_id=int(c.id), account_id=ids[0], local_date=day0) == 1
    monkeypatch.setattr(
        "src.stories.autostory_recurring.campaign_local_today", lambda **k: day0
    )
    assert reconcile_daily_from_wave_slot(
        db, campaign_id=int(c.id), wave_index=0, account_id=ids[0], local_date=day0
    )
    assert remaining_today(db, campaign_id=int(c.id), account_id=ids[0], local_date=day0) == 0
    sel = select_next_recurring_wave_accounts(db, c, wave_index=0)
    assert ids[0] not in sel["wave_account_ids"]
    update_progress(
        db,
        campaign_id=int(c.id),
        wave_index=0,
        account_id=ids[0],
        status="attempting",
    )
    row = (
        db.query(AutoStoryAccountProgress)
        .filter_by(campaign_id=int(c.id), wave_index=0, account_id=ids[0])
        .one()
    )
    assert row.status == "reconciled"


def test_recurring_wave_truncation_not_same_account_back_to_back(rec_db, monkeypatch):
    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 3)
    day0 = date(2026, 8, 26)
    c = _campaign(db, ids, spad=3, days=1, started_at=datetime(2026, 8, 26, 6, 0, 0))
    monkeypatch.setattr(
        "src.stories.autostory_recurring.campaign_local_today", lambda **k: day0
    )
    sel = select_next_recurring_wave_accounts(db, c, wave_index=0)
    assert set(sel["wave_account_ids"]) == set(ids)
    assert sel["wave_truncated"] is False


def test_allow_fewer_does_not_change_spad(monkeypatch):
    from src.stories.autostory_operator_preview import build_operator_campaign_preview
    from unittest.mock import MagicMock

    monkeypatch.setenv("AUTOSTORY_MENTIONS_PRODUCTION_CERTIFIED", "false")
    monkeypatch.setenv("MAX_STORIES_PER_ACCOUNT_PER_DAY", "3")
    monkeypatch.setattr(
        "src.stories.autostory_media.validate_campaign_media",
        lambda path: {
            "ok": True,
            "path": path,
            "error": None,
            "needs_preparation": False,
            "ready_for_story": True,
        },
    )
    preview = build_operator_campaign_preview(
        MagicMock(),
        {
            "account_ids": [1, 2, 3],
            "selection_mode": "manual",
            "media_path": "/tmp/ready.jpg",
            "caption": "hi",
            "mentions_per_story": 0,
            "campaign_mode": CAMPAIGN_MODE_RECURRING,
            "stories_per_account_per_day": 3,
            "duration_days": 7,
            "allow_fewer": True,
            "awake_start_hhmm": "10:00",
            "awake_end_hhmm": "20:00",
        },
    )
    assert preview["ok"] is True
    assert preview["stories_per_account_per_day"] == 3
    assert preview["max_story_publishes"] == 63
    assert preview["allow_fewer"] is True
    assert preview["mentions_off"] is True
    assert "planned / maximum" in preview["planned_story_publishes_label"]


def test_recurring_three_day_simulation_27_slots(rec_db, monkeypatch):
    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 3)
    start = datetime(2026, 8, 26, 6, 0, 0)
    c = _campaign(db, ids, spad=3, days=3, started_at=start)
    dates = campaign_local_dates(started_at=start, duration_days=3, tz_name="UTC")
    per_day: dict[str, dict[int, int]] = {}
    slots = 0
    keys: set[tuple[int, int, int]] = set()
    for d in dates:
        monkeypatch.setattr(
            "src.stories.autostory_recurring.campaign_local_today",
            (lambda day: (lambda **k: day))(d),
        )
        monkeypatch.setattr(
            "src.stories.daily_story_counter.production_story_day",
            (lambda day: (lambda now=None: day))(d),
        )
        for aid in ids:
            acc = db.get(Account, aid)
            acc.stories_today = 0
            acc.stories_today_on = d
        db.commit()
        day_counts = {aid: 0 for aid in ids}
        for _slot in range(3):
            wave = next_monotonic_wave_index(c)
            sel = select_next_recurring_wave_accounts(db, c, wave_index=wave)
            assert set(sel["wave_account_ids"]) == set(ids)
            assert sel["wave_truncated"] is False
            for aid in sel["wave_account_ids"]:
                key = (int(c.id), wave, aid)
                assert key not in keys
                keys.add(key)
                day_counts[aid] += 1
                slots += 1
            _apply_wave_success(db, c, ids, local_date=d, wave_index=wave)
        per_day[d.isoformat()] = day_counts
        for aid in ids:
            assert day_counts[aid] == 3
            assert remaining_today(db, campaign_id=int(c.id), account_id=aid, local_date=d) == 0
    assert slots == 27
    assert len(keys) == 27
    day4 = date(2026, 8, 29)
    monkeypatch.setattr(
        "src.stories.autostory_recurring.campaign_local_today", lambda **k: day4
    )
    sel4 = select_next_recurring_wave_accounts(db, c, wave_index=next_monotonic_wave_index(c))
    assert sel4["wave_account_ids"] == []
    assert recurring_campaign_complete(db, c) is True
    assert per_day["2026-08-26"] == {ids[0]: 3, ids[1]: 3, ids[2]: 3}
    assert per_day["2026-08-27"] == {ids[0]: 3, ids[1]: 3, ids[2]: 3}
    assert per_day["2026-08-28"] == {ids[0]: 3, ids[1]: 3, ids[2]: 3}


def test_recurring_dry_run_calendar_never_publishes(rec_db):
    from src.stories.mutation_boundary import get_provider_call_count, reset_provider_call_counter

    reset_provider_call_counter()
    before_stories = rec_db.query(AutoStoryDailyProgress).count()
    plan = plan_recurring_dry_run_calendar(
        account_ids=[101, 102, 103],
        stories_per_account_per_day=3,
        duration_days=3,
        started_at=datetime(2026, 8, 26, 6, 0, 0),
        awake_start="10:00",
        awake_end="20:00",
        tz_name="UTC",
    )
    assert plan["dry_run"] is True
    assert plan["provider_calls"] == 0
    assert plan["real_story_inserts"] == 0
    assert plan["planned_total"] == 27
    assert plan["mentions"] == "Off"
    assert len(plan["days"]) == 3
    assert plan["days"][0]["planned_total"] == 9
    assert plan["local_times"] == ["10:00", "15:00", "20:00"]
    assert get_provider_call_count() == 0
    assert rec_db.query(AutoStoryDailyProgress).count() == before_stories


def test_next_wave_respects_end_and_window():
    from src.stories.auto_story_service import next_wave_after

    now = datetime(2026, 8, 26, 9, 0, 0)
    ends = datetime(2026, 8, 28, 20, 0, 0)
    nxt = next_wave_after(now=now, times=["10:00", "15:00", "20:00"], ends_at=ends)
    assert nxt == datetime(2026, 8, 26, 10, 0, 0)
    after_end = next_wave_after(
        now=datetime(2026, 8, 28, 20, 1, 0),
        times=["10:00", "15:00", "20:00"],
        ends_at=ends,
    )
    assert after_end is None
    before_window = next_wave_after(
        now=datetime(2026, 8, 25, 12, 0, 0),
        times=["10:00"],
        ends_at=datetime(2026, 8, 26, 6, 0, 0),
    )
    assert before_window is None or before_window <= datetime(2026, 8, 26, 6, 0, 0)


# ── Wave 1C: ends_at / calendar completion hardening ───────────────────────
# started Aug 26 (mid-day), days=3 -> only Aug26/27/28 are eligible local
# dates. A wall-clock started_at+duration_days ends_at retains slack past
# Aug 28; recurring_campaign_ends_at removes that slack for recurring_daily
# without touching accounts_publish_once semantics.


def test_recurring_campaign_ends_at_boundary():
    start = datetime(2026, 8, 26, 14, 22, 0)
    ends = recurring_campaign_ends_at(started_at=start, duration_days=3, tz_name="UTC")
    assert ends == datetime(2026, 8, 29, 0, 0, 0)
    dates = campaign_local_dates(started_at=start, duration_days=3, tz_name="UTC")
    assert dates == [date(2026, 8, 26), date(2026, 8, 27), date(2026, 8, 28)]
    # Old wall-clock formula would have allowed until Aug 29 14:22 -- strictly wider.
    assert ends < start + timedelta(days=3)


def test_recurring_ends_at_blocks_slot_past_final_local_day():
    from src.stories.auto_story_service import next_wave_after

    start = datetime(2026, 8, 26, 14, 22, 0)
    ends = recurring_campaign_ends_at(started_at=start, duration_days=3, tz_name="UTC")
    times = ["10:00", "15:00", "20:00"]
    last_slot = next_wave_after(now=datetime(2026, 8, 28, 19, 0, 0), times=times, ends_at=ends)
    assert last_slot == datetime(2026, 8, 28, 20, 0, 0)
    # No Aug 29 slot is claimable, even though the old start+3d wall clock
    # (Aug 29 14:22) would still have allowed one.
    assert next_wave_after(now=last_slot, times=times, ends_at=ends) is None


def test_activate_campaign_recurring_sets_tightened_ends_at(rec_db, monkeypatch):
    from src.stories.auto_story_service import activate_campaign

    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 1)
    started = datetime(2026, 8, 26, 14, 22, 0)
    c = AutoStoryCampaign(
        status="paused",
        account_ids=list(ids),
        media_path="/tmp/cert.jpg",
        caption="cert",
        mentions_per_story=0,
        duration_days=3,
        posts_per_day=1,
        times_json=["10:00", "15:00", "20:00"],
        campaign_mode=CAMPAIGN_MODE_RECURRING,
        stories_per_account_per_day=1,
        started_at=started,
        ends_at=None,
        awake_start_hhmm="10:00",
        awake_end_hhmm="20:00",
        explicit_operator_approval=True,
        confirmation_token="I_CONFIRM_STORY_PUBLISH",
    )
    db.add(c)
    db.commit()
    monkeypatch.setattr(
        "src.stories.autostory_media.validate_campaign_media",
        lambda path: {"ok": True, "path": path, "error": None},
    )
    out = activate_campaign(
        int(c.id),
        {"explicit_operator_approval": True, "confirmation_token": "I_CONFIRM_STORY_PUBLISH"},
    )
    assert out["ok"] is True
    db.refresh(c)
    # Compare against the same helper activate_campaign wires through to
    # (operator timezone resolved internally, not hardcoded here).
    assert c.ends_at == recurring_campaign_ends_at(started_at=c.started_at, duration_days=3)


def test_activate_campaign_legacy_keeps_wall_clock_ends_at(rec_db, monkeypatch):
    """accounts_publish_once must retain its existing ends_at semantics unchanged."""
    from src.stories.auto_story_service import activate_campaign

    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 1)
    started = datetime(2026, 8, 26, 14, 22, 0)
    c = AutoStoryCampaign(
        status="paused",
        account_ids=list(ids),
        media_path="/tmp/cert.jpg",
        caption="cert",
        mentions_per_story=0,
        duration_days=3,
        posts_per_day=1,
        times_json=["12:00"],
        campaign_mode=CAMPAIGN_MODE_ONCE,
        stories_per_account_per_day=1,
        started_at=started,
        ends_at=None,
        explicit_operator_approval=True,
        confirmation_token="I_CONFIRM_STORY_PUBLISH",
    )
    db.add(c)
    db.commit()
    monkeypatch.setattr(
        "src.stories.autostory_media.validate_campaign_media",
        lambda path: {"ok": True, "path": path, "error": None},
    )
    before = datetime.utcnow()
    out = activate_campaign(
        int(c.id),
        {"explicit_operator_approval": True, "confirmation_token": "I_CONFIRM_STORY_PUBLISH"},
    )
    after = datetime.utcnow()
    assert out["ok"] is True
    db.refresh(c)
    # Unchanged legacy formula: now() + duration_days (not started_at-based).
    assert before + timedelta(days=3) <= c.ends_at <= after + timedelta(days=3)


def test_recurring_last_day_partial_completion_not_complete(rec_db, monkeypatch):
    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 2)
    start = datetime(2026, 8, 26, 6, 0, 0)
    c = _campaign(db, ids, spad=1, days=3, started_at=start)
    dates = campaign_local_dates(started_at=start, duration_days=3, tz_name="UTC")
    for d in dates[:-1]:
        for aid in ids:
            record_daily_success(db, campaign_id=int(c.id), account_id=aid, local_date=d)
    # Last day: only the first account has completed so far.
    record_daily_success(db, campaign_id=int(c.id), account_id=ids[0], local_date=dates[-1])
    assert recurring_campaign_complete(db, c) is False
    monkeypatch.setattr(
        "src.stories.autostory_recurring.campaign_local_today", lambda **k: dates[-1]
    )
    sel = select_next_recurring_wave_accounts(db, c, wave_index=5)
    assert sel["wave_account_ids"] == [ids[1]]
    assert sel["unfinished_count"] == 1

    # Completing the last account finishes the campaign cleanly on Day 3 --
    # no lingering Day 4 slack to reason about.
    record_daily_success(db, campaign_id=int(c.id), account_id=ids[1], local_date=dates[-1])
    assert recurring_campaign_complete(db, c) is True
    sel_done = select_next_recurring_wave_accounts(db, c, wave_index=6)
    assert sel_done["wave_account_ids"] == []
    assert sel_done["unfinished_count"] == 0


def test_recurring_restart_after_final_completion_is_idempotent(rec_db, monkeypatch):
    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 1)
    start = datetime(2026, 8, 26, 6, 0, 0)
    c = _campaign(db, ids, spad=1, days=2, started_at=start)
    dates = campaign_local_dates(started_at=start, duration_days=2, tz_name="UTC")
    for d in dates:
        record_daily_success(db, campaign_id=int(c.id), account_id=ids[0], local_date=d)
    assert recurring_campaign_complete(db, c) is True
    c.status = "completed"
    c.next_wave_at = None
    db.commit()

    # Simulate a worker restart: re-run selection/tick against durable state.
    # Nothing new is claimable and the completed campaign is left alone.
    monkeypatch.setattr(
        "src.stories.autostory_recurring.campaign_local_today", lambda **k: dates[-1]
    )
    sel = select_next_recurring_wave_accounts(db, c, wave_index=10)
    assert sel["wave_account_ids"] == []
    assert sel["unfinished_count"] == 0

    from src.stories.auto_story_service import tick_due_auto_story_campaigns

    tick_due_auto_story_campaigns(now=datetime.utcnow())
    db.refresh(c)
    assert c.status == "completed"
    assert c.next_wave_at is None


def test_tick_force_completes_stale_active_campaign_past_ends_at(rec_db, monkeypatch):
    """Late/post-end scheduler tick: no fresh next_wave_at, ends_at already passed."""
    from src.stories.auto_story_service import tick_due_auto_story_campaigns

    db = rec_db
    ids = _seed_accounts(db, 1)
    start = datetime(2026, 8, 26, 6, 0, 0)
    c = _campaign(db, ids, spad=1, days=1, started_at=start)
    c.next_wave_at = None
    c.ends_at = start + timedelta(days=1)
    db.commit()
    monkeypatch.setattr(
        "src.stories.auto_story_service.scheduler_story_execution_enabled", lambda: True
    )
    monkeypatch.setattr(
        "src.stories.mutation_boundary.story_mutations_enabled", lambda: True
    )
    late_now = c.ends_at + timedelta(hours=6)
    result = tick_due_auto_story_campaigns(now=late_now)
    assert result["skipped"] is False
    assert result["due_ids"] == []
    db.refresh(c)
    assert c.status == "completed"
    assert c.next_wave_at is None


def test_tick_force_completes_when_next_wave_at_exceeds_ends_at(rec_db, monkeypatch):
    """Stale next_wave_at past ends_at (pre-hardening slack) is force-corrected, not fired."""
    from src.stories.auto_story_service import tick_due_auto_story_campaigns

    db = rec_db
    ids = _seed_accounts(db, 1)
    start = datetime(2026, 8, 26, 6, 0, 0)
    c = _campaign(db, ids, spad=1, days=1, started_at=start)
    c.ends_at = start + timedelta(days=1)
    c.next_wave_at = c.ends_at + timedelta(hours=2)
    db.commit()
    monkeypatch.setattr(
        "src.stories.auto_story_service.scheduler_story_execution_enabled", lambda: True
    )
    monkeypatch.setattr(
        "src.stories.mutation_boundary.story_mutations_enabled", lambda: True
    )
    result = tick_due_auto_story_campaigns(now=c.next_wave_at + timedelta(minutes=1))
    assert result["due_ids"] == []
    db.refresh(c)
    assert c.status == "completed"
    assert c.next_wave_at is None


# ── Wave 1B: campaign-local (operator tz) vs global UTC-day capacity boundary ──
# Asia/Yerevan (UTC+4) flips to a new local calendar day ~4h before the
# global Account.stories_today UTC-day counter rolls over. See
# docs/AUTOSTORY_RECURRING_TIMEZONE_CAPACITY_BOUNDARY_20260826.md for the
# accepted policy and full analysis. These tests prove the mismatch window
# is fail-closed (may transiently block a legitimate Story) and never
# over-permissive (never allows more than the account's global daily cap).


def test_recurring_local_day_ahead_of_utc_capacity_blocks_not_overflows(rec_db, monkeypatch):
    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 1, daily_limit=3)
    acc = db.get(Account, ids[0])

    stale_utc_day = date(2026, 8, 26)  # global counter's last-rolled-over UTC day
    new_local_day = date(2026, 8, 27)  # Yerevan already flipped (UTC+4 ahead of UTC)

    # Global UTC-day counter: account already exhausted its cap on the prior
    # UTC day and the lazy-rollover clock has not yet crossed UTC midnight.
    acc.stories_today = 3
    acc.stories_today_on = stale_utc_day
    db.commit()
    monkeypatch.setattr(
        "src.stories.daily_story_counter.production_story_day",
        lambda now=None: stale_utc_day,
    )

    # Campaign-local day has already advanced to the new Yerevan date.
    c = _campaign(db, ids, spad=3, days=2, started_at=datetime(2026, 8, 26, 6, 0, 0))
    monkeypatch.setattr(
        "src.stories.autostory_recurring.campaign_local_today", lambda **k: new_local_day
    )

    campaign_rem = remaining_today(
        db, campaign_id=int(c.id), account_id=ids[0], local_date=new_local_day
    )
    assert campaign_rem == 3  # fresh local-day target, untouched
    assert platform_remaining_today(db, ids[0]) == 0  # stale UTC-day counter still exhausted

    effective = effective_remaining_today(
        db, campaign_id=int(c.id), account_id=ids[0], local_date=new_local_day
    )
    # Fail-closed: blocked for this boundary window, but never allowed past
    # the already-exhausted global cap (min() composition can only be <=
    # either side -- it cannot manufacture extra capacity).
    assert effective == 0
    sel = select_next_recurring_wave_accounts(db, c, wave_index=0)
    assert ids[0] not in sel["wave_account_ids"]
    assert sel["capacity_blocked_today"] == 1

    # Once the global UTC-day counter catches up (lazy rollover crosses UTC
    # midnight), the same local day is no longer blocked -- the mismatch is
    # a transient boundary window, not a stuck state.
    monkeypatch.setattr(
        "src.stories.daily_story_counter.production_story_day",
        lambda now=None: new_local_day,
    )
    assert platform_remaining_today(db, ids[0]) == 3
    assert (
        effective_remaining_today(
            db, campaign_id=int(c.id), account_id=ids[0], local_date=new_local_day
        )
        == 3
    )
    sel2 = select_next_recurring_wave_accounts(db, c, wave_index=0)
    assert ids[0] in sel2["wave_account_ids"]


def test_recurring_local_day_behind_utc_never_double_counts(rec_db, monkeypatch):
    """Negative-offset mirror: global UTC-day counter resets first while the
    campaign-local day (still catching up) keeps gating on its own
    already-in-progress target -- the smaller of the two always wins.
    """
    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 1, daily_limit=3)
    acc = db.get(Account, ids[0])

    still_current_local_day = date(2026, 8, 26)
    fresh_utc_day = date(2026, 8, 27)  # UTC counter already rolled over

    acc.stories_today = 0
    acc.stories_today_on = fresh_utc_day
    db.commit()
    monkeypatch.setattr(
        "src.stories.daily_story_counter.production_story_day",
        lambda now=None: fresh_utc_day,
    )

    c = _campaign(db, ids, spad=3, days=1, started_at=datetime(2026, 8, 26, 6, 0, 0))
    record_daily_success(
        db, campaign_id=int(c.id), account_id=ids[0], local_date=still_current_local_day
    )
    monkeypatch.setattr(
        "src.stories.autostory_recurring.campaign_local_today",
        lambda **k: still_current_local_day,
    )

    assert platform_remaining_today(db, ids[0]) == 3  # global side is fully fresh
    campaign_rem = remaining_today(
        db, campaign_id=int(c.id), account_id=ids[0], local_date=still_current_local_day
    )
    assert campaign_rem == 2  # 1 of 3 already used today, correctly gates the ceiling
    effective = effective_remaining_today(
        db, campaign_id=int(c.id), account_id=ids[0], local_date=still_current_local_day
    )
    assert effective == 2  # never inflated by the fresher global counter


# ── Wave 1D: provider send/persist crash window ────────────────────────────
# src/stories/publisher.py sets telegram_accepted=True immediately after
# SendStoryRequest succeeds. If the local Story-row persistence that follows
# then raises, the except block (locals().get("telegram_accepted")) returns
# ambiguous_no_retry=True / result_classification=AMBIGUOUS_NO_RETRY,
# preserving the Telegram story_id but db_id=None. No Telegram call is made
# in these tests -- they exercise the service/reconciliation boundary
# (_record_wave_progress_from_result, update_progress, record_daily_*)
# with a synthetic step shaped exactly like _finalize_multi_run's output.


def test_crash_after_send_before_persist_is_ambiguous_not_resent(rec_db, monkeypatch):
    """The catchable-exception crash path (the common case): proven safe."""
    from src.stories.auto_story_service import _record_wave_progress_from_result
    from src.stories.autostory_hardening import progress_status

    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 1)
    day0 = date(2026, 8, 26)
    c = _campaign(db, ids, spad=1, days=1, started_at=datetime(2026, 8, 26, 6, 0, 0))
    monkeypatch.setattr(
        "src.stories.autostory_recurring.campaign_local_today", lambda **k: day0
    )
    crashed_result = {
        "run_id": 9999,
        "steps": [
            {
                "account_id": ids[0],
                "ok": False,
                "ambiguous_no_retry": True,
                "status": "ambiguous_no_retry",
                "db_id": None,  # local Story row never persisted
                "story_id": 555111,  # Telegram accepted the send
                "error": "local_story_persistence_failed:OperationalError: ...",
            }
        ],
    }
    _record_wave_progress_from_result(
        db, campaign_id=int(c.id), wave_index=0, result=crashed_result
    )

    assert (
        progress_status(db, campaign_id=int(c.id), wave_index=0, account_id=ids[0])
        == "ambiguous"
    )
    assert remaining_today(db, campaign_id=int(c.id), account_id=ids[0], local_date=day0) == 0
    sel = select_next_recurring_wave_accounts(db, c, wave_index=1)
    assert ids[0] not in sel["wave_account_ids"]


def _mark_attempting_stale(db, *, campaign_id: int, wave_index: int, account_id: int, minutes: float) -> None:
    """Test helper: back-date a progress row's updated_at to simulate elapsed
    wall-clock time since a crashed worker last touched it, without faking
    the system clock (recover_stale_attempting only reads row.updated_at)."""
    row = (
        db.query(AutoStoryAccountProgress)
        .filter_by(campaign_id=int(campaign_id), wave_index=int(wave_index), account_id=int(account_id))
        .one()
    )
    row.updated_at = datetime.utcnow() - timedelta(minutes=minutes)
    db.commit()


def test_fresh_attempting_not_recovered_still_owned_by_worker(rec_db, monkeypatch):
    """A just-written attempting row (age < CLAIM_LEASE_MINUTES) may still be
    a legitimate in-flight worker -- must not be marked ambiguous yet, and
    stays selectable at the same slot (mirrors the pre-existing crash-retry
    invariant in test_recurring_crash_after_claim_retries_same_slot_not_new_slot).
    """
    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 1)
    day0 = date(2026, 8, 26)
    c = _campaign(db, ids, spad=1, days=1, started_at=datetime(2026, 8, 26, 6, 0, 0))
    update_progress(
        db, campaign_id=int(c.id), wave_index=0, account_id=ids[0], status="attempting"
    )
    monkeypatch.setattr(
        "src.stories.autostory_recurring.campaign_local_today", lambda **k: day0
    )
    sel = select_next_recurring_wave_accounts(db, c, wave_index=0)
    assert ids[0] in sel["wave_account_ids"]
    assert (
        progress_status(db, campaign_id=int(c.id), wave_index=0, account_id=ids[0])
        == "attempting"
    )


def test_stale_attempting_becomes_ambiguous_and_blocks_resend(rec_db, monkeypatch):
    """Phase 12 fix verification (replaces the former known-gap test).

    A true process kill between Telegram accepting SendStoryRequest and any
    local code running leaves AutoStoryAccountProgress at "attempting" with
    zero identifying evidence of the outcome (no story_id, no run linkage --
    see recover_stale_attempting's docstring). No Telegram-side lookup
    capability exists for Stories today (unlike the P5D message gateway's
    live reconciliation), so FOUND/ABSENT can never be positively proven for
    this row -- ambiguous (fail closed, no resend) is the only
    evidence-supported classification, covering Phase 9 scenarios A/B/C
    identically since the available evidence is identical (none) regardless
    of what actually happened on Telegram's side.
    """
    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 1)
    day0 = date(2026, 8, 26)
    c = _campaign(db, ids, spad=1, days=1, started_at=datetime(2026, 8, 26, 6, 0, 0))
    update_progress(
        db, campaign_id=int(c.id), wave_index=0, account_id=ids[0], status="attempting"
    )
    _mark_attempting_stale(db, campaign_id=int(c.id), wave_index=0, account_id=ids[0], minutes=45)
    monkeypatch.setattr(
        "src.stories.autostory_recurring.campaign_local_today", lambda **k: day0
    )

    assert remaining_today(db, campaign_id=int(c.id), account_id=ids[0], local_date=day0) == 1
    sel = select_next_recurring_wave_accounts(db, c, wave_index=0)
    assert ids[0] not in sel["wave_account_ids"]  # STALE_ATTEMPTING_BLIND_RESEND=BLOCKED
    assert (
        progress_status(db, campaign_id=int(c.id), wave_index=0, account_id=ids[0])
        == "ambiguous"
    )


def test_stale_attempting_recovery_reflects_into_daily_progress(rec_db, monkeypatch):
    """Phase 7: recovered ambiguous state must consume the daily target
    exactly once via the existing reconcile_daily_from_wave_slot path.
    """
    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 1)
    day0 = date(2026, 8, 26)
    c = _campaign(db, ids, spad=3, days=1, started_at=datetime(2026, 8, 26, 6, 0, 0))
    update_progress(
        db, campaign_id=int(c.id), wave_index=0, account_id=ids[0], status="attempting"
    )
    _mark_attempting_stale(db, campaign_id=int(c.id), wave_index=0, account_id=ids[0], minutes=31)
    monkeypatch.setattr(
        "src.stories.autostory_recurring.campaign_local_today", lambda **k: day0
    )
    select_next_recurring_wave_accounts(db, c, wave_index=0)
    row = get_daily_row(db, campaign_id=int(c.id), account_id=ids[0], local_date=day0)
    assert row.ambiguous_count == 1
    assert row.completed_for_day is True
    assert remaining_today(db, campaign_id=int(c.id), account_id=ids[0], local_date=day0) == 0
    r = record_daily_success(db, campaign_id=int(c.id), account_id=ids[0], local_date=day0)
    assert r["ok"] is False  # blind resend into the daily target is still refused


def test_stale_attempting_recovery_is_idempotent(rec_db, monkeypatch):
    """Phase 9-D: repeated recovery must not double-count."""
    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 1)
    day0 = date(2026, 8, 26)
    c = _campaign(db, ids, spad=1, days=1, started_at=datetime(2026, 8, 26, 6, 0, 0))
    update_progress(
        db, campaign_id=int(c.id), wave_index=0, account_id=ids[0], status="attempting"
    )
    _mark_attempting_stale(db, campaign_id=int(c.id), wave_index=0, account_id=ids[0], minutes=60)
    monkeypatch.setattr(
        "src.stories.autostory_recurring.campaign_local_today", lambda **k: day0
    )
    select_next_recurring_wave_accounts(db, c, wave_index=0)
    select_next_recurring_wave_accounts(db, c, wave_index=0)
    select_next_recurring_wave_accounts(db, c, wave_index=0)
    row = get_daily_row(db, campaign_id=int(c.id), account_id=ids[0], local_date=day0)
    assert row.ambiguous_count == 1
    assert (
        progress_status(db, campaign_id=int(c.id), wave_index=0, account_id=ids[0])
        == "ambiguous"
    )


def test_stale_attempting_recovery_race_two_ticks_no_double_count(rec_db, monkeypatch):
    """Phase 9-E concurrency proxy.

    True multi-process concurrency isn't reproducible against a single
    in-memory SQLite session (the same constraint applies to every other
    claim-race test in this suite, e.g. test_recurring_no_duplicate_scheduler_tick,
    which verifies the atomic compare-and-set UPDATE rather than real
    threads). The atomic campaign claim in claim_campaign already guarantees
    only one worker at a time can reach wave execution/selection for a given
    campaign; this test proves that even if two "ticks" both run recovery
    against the same stale row back-to-back (the worst case the claim
    mechanism allows), the transition and daily counters stay single-valued
    -- no resend is triggered by either.
    """
    from src.stories.autostory_hardening import recover_stale_attempting

    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 1)
    day0 = date(2026, 8, 26)
    c = _campaign(db, ids, spad=1, days=1, started_at=datetime(2026, 8, 26, 6, 0, 0))
    update_progress(
        db, campaign_id=int(c.id), wave_index=0, account_id=ids[0], status="attempting"
    )
    _mark_attempting_stale(db, campaign_id=int(c.id), wave_index=0, account_id=ids[0], minutes=31)

    first = recover_stale_attempting(db, campaign_id=int(c.id), wave_index=0, account_id=ids[0])
    second = recover_stale_attempting(db, campaign_id=int(c.id), wave_index=0, account_id=ids[0])
    assert first == "ambiguous"
    assert second is None  # already resolved -- nothing left for a second "worker" to do
    assert (
        progress_status(db, campaign_id=int(c.id), wave_index=0, account_id=ids[0])
        == "ambiguous"
    )


def test_restart_after_stale_recovery_not_reselected(rec_db, monkeypatch):
    """Phase 9-F: reconciled/ambiguous outcomes survive a simulated restart."""
    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 1)
    day0 = date(2026, 8, 26)
    c = _campaign(db, ids, spad=1, days=1, started_at=datetime(2026, 8, 26, 6, 0, 0))
    update_progress(
        db, campaign_id=int(c.id), wave_index=0, account_id=ids[0], status="attempting"
    )
    _mark_attempting_stale(db, campaign_id=int(c.id), wave_index=0, account_id=ids[0], minutes=31)
    monkeypatch.setattr(
        "src.stories.autostory_recurring.campaign_local_today", lambda **k: day0
    )
    select_next_recurring_wave_accounts(db, c, wave_index=0)  # crashed worker's tick recovers it

    # Simulate a fresh process/worker restart: a brand new scheduler tick.
    sel_after_restart = select_next_recurring_wave_accounts(db, c, wave_index=0)
    assert ids[0] not in sel_after_restart["wave_account_ids"]


def test_restart_after_ambiguous_no_blind_resend(rec_db, monkeypatch):
    """Phase 9-G: the pre-existing exception-caught ambiguous path (not the
    stale-attempting recovery path) is also restart-safe -- both crash
    classification routes converge to the same no-resend guarantee.
    """
    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 1)
    day0 = date(2026, 8, 26)
    c = _campaign(db, ids, spad=1, days=1, started_at=datetime(2026, 8, 26, 6, 0, 0))
    update_progress(
        db,
        campaign_id=int(c.id),
        wave_index=0,
        account_id=ids[0],
        status="ambiguous",
        telegram_story_id=555111,
    )
    record_daily_ambiguous(db, campaign_id=int(c.id), account_id=ids[0], local_date=day0)
    monkeypatch.setattr(
        "src.stories.autostory_recurring.campaign_local_today", lambda **k: day0
    )
    sel = select_next_recurring_wave_accounts(db, c, wave_index=0)
    assert ids[0] not in sel["wave_account_ids"]


def test_ambiguous_day_one_does_not_block_day_two(rec_db, monkeypatch):
    """Phase 9-H: an uncertain Day 1 slot must not silently ban the account
    from the campaign forever -- only that day's target is consumed.
    """
    _cert_ok(monkeypatch)
    db = rec_db
    ids = _seed_accounts(db, 1)
    day0, day1 = date(2026, 8, 26), date(2026, 8, 27)
    c = _campaign(db, ids, spad=1, days=2, started_at=datetime(2026, 8, 26, 6, 0, 0))
    update_progress(
        db, campaign_id=int(c.id), wave_index=0, account_id=ids[0], status="attempting"
    )
    _mark_attempting_stale(db, campaign_id=int(c.id), wave_index=0, account_id=ids[0], minutes=31)
    monkeypatch.setattr(
        "src.stories.autostory_recurring.campaign_local_today", lambda **k: day0
    )
    select_next_recurring_wave_accounts(db, c, wave_index=0)
    assert remaining_today(db, campaign_id=int(c.id), account_id=ids[0], local_date=day0) == 0

    # Day 2: fresh daily row, fresh wave_index -- no lingering ban.
    monkeypatch.setattr(
        "src.stories.autostory_recurring.campaign_local_today", lambda **k: day1
    )
    assert remaining_today(db, campaign_id=int(c.id), account_id=ids[0], local_date=day1) == 1
    sel_day2 = select_next_recurring_wave_accounts(db, c, wave_index=1)
    assert ids[0] in sel_day2["wave_account_ids"]
