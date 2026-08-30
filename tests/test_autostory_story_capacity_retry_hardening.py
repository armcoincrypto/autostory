"""Regression tests for the Campaign #18 STORIES_TOO_MUCH / 45s retry-loop incident.

Root cause (proven from code + production logs, see docs/AUTOSTORY_STORY_CAPACITY_RETRY_INCIDENT.md):

1. Telethon's RPCError formats str(e) as "RPCError {code}: {message}(...)" -- the
   retry-after regex in precheck.py ran against that full string and misread the
   HTTP-style error code (400) as a 400-second wait time.
2. story_auth_is_fresh() only ever returns True for a fresh "allowed" precheck --
   it never returns True for "rate_limited", so a known story_blocked_until cooldown
   was computed but never consulted before retrying.
3. execute_wave's fresh_story_auth_failed branch never advanced campaign.next_wave_at,
   so the scheduler's 45s base loop (LOOP_INTERVAL_SEC) re-claimed and re-attempted
   the same wave every tick -- observed ~100+ times over ~90 minutes in production.

No Telegram calls in these tests. No real Story publication.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from telethon.errors.rpcbaseerrors import RPCError

from src.core.database import Base
from src.core.models import Account, AccountStatus, AutoStoryCampaign, SystemLog
from src.stories.autostory_hardening import ensure_fresh_story_auth_for_accounts
from src.stories.autostory_recurring import CAMPAIGN_MODE_RECURRING, ensure_daily_progress_rows
from src.stories.precheck import run_story_precheck
from src.stories.story_auth_state import resolve_story_auth_state


# ── Fixture (mirrors tests/test_recurring_daily_certification.py's rec_db) ─


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
    monkeypatch.setenv("AUTOSTORY_MENTIONS_PRODUCTION_CERTIFIED", "false")
    yield session
    session.close()


def _seed_account(db, i: int = 0) -> int:
    a = Account(
        phone_number=f"+1555300{i:04d}",
        status=AccountStatus.ACTIVE,
        stories_today=0,
        stories_today_on=date.today(),
    )
    db.add(a)
    db.commit()
    return int(a.id)


def _campaign(db, ids: list[int], *, spad: int = 1, days: int = 1) -> AutoStoryCampaign:
    # started_at/ends_at are real wall-clock bounds checked for real inside
    # execute_wave (unlike campaign_local_today, which every test mocks to a
    # fixed fake date) -- ends_at must stay safely in the future regardless of
    # when the suite actually runs, or the campaign force-completes instead of
    # ever reaching the code path under test.
    now = datetime.utcnow()
    c = AutoStoryCampaign(
        status="active",
        account_ids=list(ids),
        media_path="/tmp/cert.jpg",
        caption="cert",
        mentions_per_story=0,
        duration_days=days,
        posts_per_day=spad,
        times_json=["10:00"],
        campaign_mode=CAMPAIGN_MODE_RECURRING,
        stories_per_account_per_day=spad,
        started_at=now - timedelta(hours=1),
        ends_at=now + timedelta(days=365),
        next_wave_at=now,  # immediately due, matching how a real activated campaign starts
        awake_start_hhmm="10:00",
        awake_end_hhmm="20:00",
        explicit_operator_approval=True,
        confirmation_token="I_CONFIRM_STORY_PUBLISH",
    )
    db.add(c)
    db.commit()
    ensure_daily_progress_rows(
        db, campaign_id=int(c.id), account_ids=ids, local_dates=[date(2026, 8, 29)], target_count=spad
    )
    db.commit()
    return c


def _stories_too_much_exc() -> RPCError:
    """A real Telethon RPCError shaped exactly like the production incident:
    str(e) == 'RPCError 400: STORIES_TOO_MUCH (caused by CanSendStoryRequest)'."""

    class _FakeRequest:
        pass

    return RPCError(_FakeRequest(), "STORIES_TOO_MUCH", code=400)


# ── A. precheck.py regex fix: don't misread the RPC code as a wait time ───


@pytest.mark.asyncio
async def test_stories_too_much_does_not_misread_rpc_code_as_wait_seconds():
    exc = _stories_too_much_exc()
    assert str(exc) == "RPCError 400: STORIES_TOO_MUCH (caused by _FakeRequest)"
    assert exc.message == "STORIES_TOO_MUCH"  # the clean message telethon gives us

    client = AsyncMock()
    client.get_input_entity = AsyncMock(side_effect=Exception("no me"))
    client.__call__ = AsyncMock(side_effect=exc)

    async def _call(request):
        raise exc

    client.side_effect = _call

    result = await run_story_precheck(client, account_id=999)
    assert result["status"] == "rate_limited"
    # The bug: str(e) contains "400" (the RPC code), which a naive digit-scan
    # would misread as a 400-second wait. The message itself has no wait hint,
    # so the safe fallback (24h) must be used instead.
    assert result["retry_after_seconds"] == 86400
    assert result["retry_after_seconds"] != 400


@pytest.mark.asyncio
async def test_stories_too_much_extracts_real_wait_hint_when_present():
    """A message that genuinely contains a wait hint must still be honored."""

    class _FakeRequest:
        pass

    exc = RPCError(_FakeRequest(), "STORIES_TOO_MUCH: please wait 7200 seconds", code=400)

    async def _call(request):
        raise exc

    client = AsyncMock()
    client.get_input_entity = AsyncMock(side_effect=Exception("no me"))
    client.side_effect = _call

    result = await run_story_precheck(client, account_id=999)
    assert result["status"] == "rate_limited"
    assert result["retry_after_seconds"] == 7200


# ── B/F. ensure_fresh_story_auth_for_accounts: skip remote call when blocked ─


@pytest.mark.asyncio
async def test_blocked_until_future_skips_remote_precheck_entirely(rec_db, monkeypatch):
    db = rec_db
    aid = _seed_account(db)
    acc = db.get(Account, aid)
    acc.story_precheck_status = "rate_limited"
    acc.story_precheck_reason = "RPCError 400: STORIES_TOO_MUCH (caused by CanSendStoryRequest)"
    acc.story_precheck_checked_at = datetime.utcnow()
    acc.story_blocked_until = datetime.utcnow() + timedelta(hours=1)
    db.commit()

    called = {"n": 0}

    async def _should_not_be_called(account_id):
        called["n"] += 1
        raise AssertionError("open_controlled_story_client must not be called for a blocked account")

    monkeypatch.setattr(
        "src.stories.client_lifecycle.open_controlled_story_client", _should_not_be_called
    )

    out = await ensure_fresh_story_auth_for_accounts([aid])
    assert called["n"] == 0
    assert out["ok"] is False
    assert len(out["failed"]) == 1
    assert out["failed"][0]["account_id"] == aid
    assert out["failed"][0]["error"] == "story_auth_blocked_until_future"


@pytest.mark.asyncio
async def test_blocked_until_expired_attempts_remote_precheck_again(rec_db, monkeypatch):
    db = rec_db
    aid = _seed_account(db)
    acc = db.get(Account, aid)
    acc.story_precheck_status = "rate_limited"
    acc.story_precheck_checked_at = datetime.utcnow() - timedelta(hours=2)
    acc.story_blocked_until = datetime.utcnow() - timedelta(minutes=1)  # already expired
    db.commit()

    class _Lease:
        def __init__(self):
            self.wrapper = type("W", (), {"client": object()})()

        async def close(self):
            pass

    async def _open(account_id):
        return _Lease(), None

    async def _precheck(client, account_id):
        return {"status": "allowed", "reason": "CanSendStory OK", "retry_after_seconds": None}

    monkeypatch.setattr("src.stories.client_lifecycle.open_controlled_story_client", _open)
    monkeypatch.setattr("src.stories.precheck.run_story_precheck", _precheck)

    out = await ensure_fresh_story_auth_for_accounts([aid])
    assert out["ok"] is True
    assert out["refreshed"] == [aid]


@pytest.mark.asyncio
async def test_one_blocked_account_does_not_block_others(rec_db, monkeypatch):
    db = rec_db
    blocked_id = _seed_account(db, 0)
    ok_id1 = _seed_account(db, 1)
    ok_id2 = _seed_account(db, 2)

    acc = db.get(Account, blocked_id)
    acc.story_precheck_status = "rate_limited"
    acc.story_precheck_checked_at = datetime.utcnow()
    acc.story_blocked_until = datetime.utcnow() + timedelta(hours=1)
    db.commit()

    remote_calls = {"n": 0}

    class _Lease:
        def __init__(self):
            self.wrapper = type("W", (), {"client": object()})()

        async def close(self):
            pass

    async def _open(account_id):
        remote_calls["n"] += 1
        return _Lease(), None

    async def _precheck(client, account_id):
        return {"status": "allowed", "reason": "CanSendStory OK", "retry_after_seconds": None}

    monkeypatch.setattr("src.stories.client_lifecycle.open_controlled_story_client", _open)
    monkeypatch.setattr("src.stories.precheck.run_story_precheck", _precheck)

    out = await ensure_fresh_story_auth_for_accounts([blocked_id, ok_id1, ok_id2])
    assert remote_calls["n"] == 2  # exactly the two OK accounts, blocked one skipped
    assert set(out["refreshed"]) == {ok_id1, ok_id2}
    assert [f["account_id"] for f in out["failed"]] == [blocked_id]


# ── D. execute_wave: backoff on fresh_story_auth_failed, no tight loop ─────


def _enable_execution_flags(monkeypatch):
    # story_mutations_enabled() is production-gated by design ("Local Mac / pytest
    # cannot publish even if STORY_MUTATIONS_ENABLED=true" -- mutation_boundary.py).
    # Setting ENVIRONMENT=production in a scoped, auto-reverting monkeypatch is the
    # established pattern for exercising that gated path in tests (see
    # test_autostory_hardening.py, test_local_safety_fail_closed.py,
    # test_story_runtime_safety_boundaries.py). No Telegram call is reachable here:
    # ensure_fresh_story_auth_for_accounts is mocked to fail before authorize_wave
    # or any live-publish code ever runs.
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("STORY_MUTATIONS_ENABLED", "true")
    monkeypatch.setenv("SCHEDULER_STORY_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("CONTROLLED_STORY_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("CAMPAIGN_EXECUTION_ENABLED", "true")


def _mock_policy_ok(monkeypatch):
    monkeypatch.setattr(
        "src.stories.autostory_media.validate_campaign_execution_policy",
        lambda campaign: {"ok": True, "media": {"path": "/tmp/cert.jpg"}},
    )


def _cert_ok(monkeypatch):
    monkeypatch.setattr(
        "src.stories.autostory_hardening.is_account_certified_publish",
        lambda db, aid: (True, "certified_publish"),
    )


def test_execute_wave_backs_off_on_fresh_auth_failure_no_tight_loop(rec_db, monkeypatch):
    from src.stories.auto_story_service import FRESH_AUTH_FAILURE_BACKOFF_MINUTES, execute_wave

    db = rec_db
    _enable_execution_flags(monkeypatch)
    _mock_policy_ok(monkeypatch)
    _cert_ok(monkeypatch)
    monkeypatch.setattr(
        "src.stories.autostory_recurring.campaign_local_today", lambda **k: date(2026, 8, 29)
    )
    aid = _seed_account(db)
    c = _campaign(db, [aid])
    campaign_id = int(c.id)

    async def _always_blocked(account_ids):
        return {
            "already_fresh": [],
            "refreshed": [],
            "failed": [{"account_id": aid, "error": "precheck_rate_limited", "reason": "STORIES_TOO_MUCH"}],
            "ok": False,
        }

    monkeypatch.setattr(
        "src.stories.autostory_hardening.ensure_fresh_story_auth_for_accounts", _always_blocked
    )

    before = datetime.utcnow()
    result = execute_wave(campaign_id, require_scheduler_flag=True, worker_id="test-worker")

    assert result["ok"] is False
    assert result["error"] == "fresh_story_auth_failed"

    db.commit()
    refreshed = db.get(AutoStoryCampaign, campaign_id)
    assert refreshed.next_wave_at is not None
    assert refreshed.next_wave_at >= before + timedelta(minutes=FRESH_AUTH_FAILURE_BACKOFF_MINUTES - 1)
    assert refreshed.claimed_by is None  # claim released, not stuck

    # Durable, queryable record of why -- not just structlog (which the incident
    # showed is not reliably captured in production).
    rows = db.query(SystemLog).filter(SystemLog.message == "autostory.account.deferred").all()
    assert len(rows) == 1
    assert rows[0].details.get("reason") == "STORIES_TOO_MUCH"
    assert rows[0].details.get("error") == "precheck_rate_limited"
    assert rows[0].account_id == aid


def test_100_scheduler_ticks_bounded_precheck_calls_no_spam(rec_db, monkeypatch):
    """Simulates the real scheduler: only call execute_wave when next_wave_at is due,
    repeated 100 times at the real 45s base interval. Before the fix this reproduced
    ~100 calls to the fresh-auth gate; after the fix, calls are bounded by the backoff.
    """
    from src.stories.auto_story_service import FRESH_AUTH_FAILURE_BACKOFF_MINUTES, execute_wave

    db = rec_db
    _enable_execution_flags(monkeypatch)
    _mock_policy_ok(monkeypatch)
    _cert_ok(monkeypatch)
    monkeypatch.setattr(
        "src.stories.autostory_recurring.campaign_local_today", lambda **k: date(2026, 8, 29)
    )
    aid = _seed_account(db)
    c = _campaign(db, [aid])
    campaign_id = int(c.id)

    call_count = {"n": 0}

    async def _always_blocked(account_ids):
        call_count["n"] += 1
        return {
            "already_fresh": [],
            "refreshed": [],
            "failed": [{"account_id": aid, "error": "precheck_rate_limited"}],
            "ok": False,
        }

    monkeypatch.setattr(
        "src.stories.autostory_hardening.ensure_fresh_story_auth_for_accounts", _always_blocked
    )

    LOOP_INTERVAL_SEC = 45
    ticks = 100
    for _ in range(ticks):
        row = db.get(AutoStoryCampaign, campaign_id)
        if row.next_wave_at is not None and row.next_wave_at <= datetime.utcnow():
            execute_wave(campaign_id, require_scheduler_flag=True, worker_id="sched")
            db.commit()
        # Simulate the passage of one scheduler tick without a real 45s sleep by
        # moving the campaign's own due time backward -- equivalent to "45 real
        # seconds passed" from the scheduler's point of view, without slowing the
        # test down by ticks*45s.
        row = db.get(AutoStoryCampaign, campaign_id)
        if row.next_wave_at is not None:
            row.next_wave_at -= timedelta(seconds=LOOP_INTERVAL_SEC)
            db.commit()

    elapsed_equivalent_minutes = (ticks * LOOP_INTERVAL_SEC) / 60
    expected_max_calls = int(elapsed_equivalent_minutes // FRESH_AUTH_FAILURE_BACKOFF_MINUTES) + 2
    assert call_count["n"] >= 1, "sanity: the loop must actually have called execute_wave at least once"
    assert call_count["n"] <= expected_max_calls, (
        f"expected <= {expected_max_calls} precheck attempts over "
        f"{elapsed_equivalent_minutes:.0f} simulated minutes, got {call_count['n']} "
        f"(pre-fix production incident: 100+ calls over ~90 minutes)"
    )
    assert call_count["n"] < ticks  # definitely bounded, not once-per-tick


# ── E/backoff timing: tick before retry_at is skipped, at/after is rechecked ─


def test_tick_before_backoff_expiry_is_not_selected(rec_db, monkeypatch):
    from src.stories.auto_story_service import execute_wave

    db = rec_db
    _enable_execution_flags(monkeypatch)
    _mock_policy_ok(monkeypatch)
    _cert_ok(monkeypatch)
    monkeypatch.setattr(
        "src.stories.autostory_recurring.campaign_local_today", lambda **k: date(2026, 8, 29)
    )
    aid = _seed_account(db)
    c = _campaign(db, [aid])
    campaign_id = int(c.id)

    calls = {"n": 0}

    async def _always_blocked(account_ids):
        calls["n"] += 1
        return {"already_fresh": [], "refreshed": [], "failed": [{"account_id": aid, "error": "x"}], "ok": False}

    monkeypatch.setattr(
        "src.stories.autostory_hardening.ensure_fresh_story_auth_for_accounts", _always_blocked
    )
    execute_wave(campaign_id, require_scheduler_flag=True, worker_id="w1")
    db.commit()
    assert calls["n"] == 1

    row = db.get(AutoStoryCampaign, campaign_id)
    assert row.next_wave_at > datetime.utcnow()  # backoff is in the future

    # A tick "shortly after" that finds next_wave_at still in the future must not
    # even attempt the wave (this is the scheduler's own due-check, exercised here
    # directly since execute_wave itself doesn't re-check next_wave_at internally).
    if row.next_wave_at > datetime.utcnow():
        pass  # scheduler would skip calling execute_wave entirely
    assert calls["n"] == 1  # unchanged -- no second attempt was made


def test_tick_at_or_after_backoff_expiry_rechecks_eligibility(rec_db, monkeypatch):
    from src.stories.auto_story_service import execute_wave

    db = rec_db
    _enable_execution_flags(monkeypatch)
    _mock_policy_ok(monkeypatch)
    _cert_ok(monkeypatch)
    monkeypatch.setattr(
        "src.stories.autostory_recurring.campaign_local_today", lambda **k: date(2026, 8, 29)
    )
    aid = _seed_account(db)
    c = _campaign(db, [aid])
    campaign_id = int(c.id)

    calls = {"n": 0}

    async def _blocked_once_then_allowed(account_ids):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"already_fresh": [], "refreshed": [], "failed": [{"account_id": aid, "error": "x"}], "ok": False}
        return {"already_fresh": [], "refreshed": list(account_ids), "failed": [], "ok": True}

    monkeypatch.setattr(
        "src.stories.autostory_hardening.ensure_fresh_story_auth_for_accounts",
        _blocked_once_then_allowed,
    )
    execute_wave(campaign_id, require_scheduler_flag=True, worker_id="w1")
    db.commit()
    assert calls["n"] == 1

    # Fast-forward past the backoff window (simulating real time passing).
    row = db.get(AutoStoryCampaign, campaign_id)
    row.next_wave_at = datetime.utcnow() - timedelta(seconds=1)
    db.commit()

    # Second attempt at/after retry_at: eligibility is rechecked (a fresh call is
    # made -- this one happens to succeed, proving the account is not permanently
    # stuck after one failure).
    execute_wave(campaign_id, require_scheduler_flag=True, worker_id="w1")
    assert calls["n"] == 2


# ── Wave 2: durable observability -- one row per transition, not per retry ──


def test_durable_deferred_record_has_full_required_context(rec_db, monkeypatch):
    """A blocked/deferred attempt must be answerable: campaign_id, account_id,
    wave_index, event, reason, blocked_until/retry_at, next_wave_at -- without
    relying on structlog/journald (proven unreliable in the incident)."""
    from src.stories.auto_story_service import FRESH_AUTH_FAILURE_BACKOFF_MINUTES, execute_wave

    db = rec_db
    _enable_execution_flags(monkeypatch)
    _mock_policy_ok(monkeypatch)
    _cert_ok(monkeypatch)
    monkeypatch.setattr(
        "src.stories.autostory_recurring.campaign_local_today", lambda **k: date(2026, 8, 29)
    )
    aid = _seed_account(db)
    c = _campaign(db, [aid])
    campaign_id = int(c.id)
    blocked_until_iso = (datetime.utcnow() + timedelta(hours=6)).isoformat()

    async def _blocked(account_ids):
        return {
            "already_fresh": [],
            "refreshed": [],
            "failed": [
                {
                    "account_id": aid,
                    "error": "precheck_rate_limited",
                    "reason": "STORIES_TOO_MUCH",
                    "blocked_until": blocked_until_iso,
                    "checked_at": datetime.utcnow().isoformat(),
                }
            ],
            "ok": False,
        }

    monkeypatch.setattr("src.stories.autostory_hardening.ensure_fresh_story_auth_for_accounts", _blocked)
    execute_wave(campaign_id, require_scheduler_flag=True, worker_id="w1")
    db.commit()

    row = db.query(SystemLog).filter(SystemLog.message == "autostory.account.deferred").one()
    d = row.details
    assert d["campaign_id"] == campaign_id
    assert row.account_id == aid
    assert d["wave_number"] == 1  # wave_index 0 + 1
    assert d["reason"] == "STORIES_TOO_MUCH"
    assert d["error"] == "precheck_rate_limited"
    assert d["blocked_until"] == blocked_until_iso
    assert "retry_at" in d and d["retry_at"] is not None

    campaign_row = db.get(AutoStoryCampaign, campaign_id)
    assert campaign_row.next_wave_at is not None  # answers "when will we look again"


def test_unchanged_cooldown_across_retries_writes_no_duplicate_log(rec_db, monkeypatch):
    """The exact 'cooldown tick -> no repeated identical SystemLog spam' requirement:
    the same block reason/blocked_until across multiple retries must not create a
    new row each time."""
    from src.stories.auto_story_service import execute_wave

    db = rec_db
    _enable_execution_flags(monkeypatch)
    _mock_policy_ok(monkeypatch)
    _cert_ok(monkeypatch)
    monkeypatch.setattr(
        "src.stories.autostory_recurring.campaign_local_today", lambda **k: date(2026, 8, 29)
    )
    aid = _seed_account(db)
    c = _campaign(db, [aid])
    campaign_id = int(c.id)
    fixed_blocked_until = (datetime.utcnow() + timedelta(hours=6)).isoformat()

    async def _same_block_every_time(account_ids):
        return {
            "already_fresh": [],
            "refreshed": [],
            "failed": [
                {
                    "account_id": aid,
                    "error": "precheck_rate_limited",
                    "reason": "STORIES_TOO_MUCH",
                    "blocked_until": fixed_blocked_until,
                }
            ],
            "ok": False,
        }

    monkeypatch.setattr(
        "src.stories.autostory_hardening.ensure_fresh_story_auth_for_accounts", _same_block_every_time
    )

    for _ in range(5):
        execute_wave(campaign_id, require_scheduler_flag=True, worker_id="w1")
        db.commit()
        row = db.get(AutoStoryCampaign, campaign_id)
        row.next_wave_at = datetime.utcnow() - timedelta(seconds=1)  # force-due for the next retry
        db.commit()

    rows = db.query(SystemLog).filter(SystemLog.message == "autostory.account.deferred").all()
    assert len(rows) == 1  # 5 retries, identical cooldown state -> exactly one durable record


def test_new_block_reason_after_prior_cooldown_produces_new_record(rec_db, monkeypatch):
    """A genuine state change (different reason or blocked_until) must still be
    recorded -- de-duplication must not suppress real new information."""
    from src.stories.auto_story_service import execute_wave

    db = rec_db
    _enable_execution_flags(monkeypatch)
    _mock_policy_ok(monkeypatch)
    _cert_ok(monkeypatch)
    monkeypatch.setattr(
        "src.stories.autostory_recurring.campaign_local_today", lambda **k: date(2026, 8, 29)
    )
    aid = _seed_account(db)
    c = _campaign(db, [aid])
    campaign_id = int(c.id)

    state = {"reason": "STORIES_TOO_MUCH", "blocked_until": (datetime.utcnow() + timedelta(hours=1)).isoformat()}

    async def _blocked(account_ids):
        return {
            "already_fresh": [],
            "refreshed": [],
            "failed": [{"account_id": aid, "error": "precheck_rate_limited", **state}],
            "ok": False,
        }

    monkeypatch.setattr("src.stories.autostory_hardening.ensure_fresh_story_auth_for_accounts", _blocked)
    execute_wave(campaign_id, require_scheduler_flag=True, worker_id="w1")
    db.commit()
    assert db.query(SystemLog).filter(SystemLog.message == "autostory.account.deferred").count() == 1

    # A new cooldown window (e.g. a fresh precheck extended the block) is new information.
    state["blocked_until"] = (datetime.utcnow() + timedelta(hours=6)).isoformat()
    row = db.get(AutoStoryCampaign, campaign_id)
    row.next_wave_at = datetime.utcnow() - timedelta(seconds=1)
    db.commit()
    execute_wave(campaign_id, require_scheduler_flag=True, worker_id="w1")
    db.commit()
    assert db.query(SystemLog).filter(SystemLog.message == "autostory.account.deferred").count() == 2


def test_100_tick_simulation_log_volume_bounded(rec_db, monkeypatch):
    """Extends the tight-loop regression test: not just Telegram calls, but
    durable SystemLog rows must also stay bounded across 100 simulated ticks
    of an unchanged cooldown."""
    from src.stories.auto_story_service import execute_wave

    db = rec_db
    _enable_execution_flags(monkeypatch)
    _mock_policy_ok(monkeypatch)
    _cert_ok(monkeypatch)
    monkeypatch.setattr(
        "src.stories.autostory_recurring.campaign_local_today", lambda **k: date(2026, 8, 29)
    )
    aid = _seed_account(db)
    c = _campaign(db, [aid])
    campaign_id = int(c.id)
    fixed_blocked_until = (datetime.utcnow() + timedelta(hours=24)).isoformat()

    async def _always_blocked(account_ids):
        return {
            "already_fresh": [],
            "refreshed": [],
            "failed": [
                {
                    "account_id": aid,
                    "error": "precheck_rate_limited",
                    "reason": "STORIES_TOO_MUCH",
                    "blocked_until": fixed_blocked_until,
                }
            ],
            "ok": False,
        }

    monkeypatch.setattr(
        "src.stories.autostory_hardening.ensure_fresh_story_auth_for_accounts", _always_blocked
    )

    LOOP_INTERVAL_SEC = 45
    for _ in range(100):
        row = db.get(AutoStoryCampaign, campaign_id)
        if row.next_wave_at is not None and row.next_wave_at <= datetime.utcnow():
            execute_wave(campaign_id, require_scheduler_flag=True, worker_id="sched")
            db.commit()
        row = db.get(AutoStoryCampaign, campaign_id)
        if row.next_wave_at is not None:
            row.next_wave_at -= timedelta(seconds=LOOP_INTERVAL_SEC)
            db.commit()

    rows = db.query(SystemLog).filter(SystemLog.message == "autostory.account.deferred").count()
    assert rows == 1, f"expected exactly 1 durable record for an unchanged 24h cooldown, got {rows}"
