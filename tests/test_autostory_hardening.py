"""MODEL A AutoStory hardening regression tests (isolated SQLite)."""
from __future__ import annotations

from datetime import datetime, timedelta
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.core.database import Base
from src.core.models import Account, AutoStoryCampaign, AutoStoryAccountProgress
from src.stories.autostory_hardening import (
    MAX_AUTOSTORY_WAVE_SIZE,
    authorize_wave,
    claim_campaign,
    ensure_progress_row,
    plan_full_fleet_waves,
    release_campaign_claim,
    revoke_wave_authorization,
    update_progress,
    wave_size_ok,
)


@pytest.fixture()
def hard_db(monkeypatch):
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
    monkeypatch.setattr("src.stories.autostory_hardening.get_db_context", _ctx, raising=False)
    yield session
    session.close()


def _mk_campaign(db, account_ids):
    c = AutoStoryCampaign(
        status="active",
        account_ids=list(account_ids),
        media_path="/tmp/x.jpg",
        caption="t",
        mentions_per_story=0,
        duration_days=1,
        posts_per_day=1,
        times_json=["12:00"],
        started_at=datetime.utcnow(),
        ends_at=datetime.utcnow() + timedelta(days=1),
        next_wave_at=datetime.utcnow() - timedelta(minutes=1),
        explicit_operator_approval=True,
        confirmation_token="I_CONFIRM_STORY_PUBLISH",
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def test_wave_size_25_ok():
    assert wave_size_ok(list(range(25))) == (True, None)


def test_wave_size_26_rejected():
    ok, err = wave_size_ok(list(range(26)))
    assert ok is False
    assert err == "WAVE_SIZE_EXCEEDS_MAX"
    assert MAX_AUTOSTORY_WAVE_SIZE == 25


def test_two_workers_claim_same_campaign(hard_db):
    c = _mk_campaign(hard_db, [1, 2, 3])
    w1, _ = claim_campaign(hard_db, c.id, worker_id="A")
    w2, m2 = claim_campaign(hard_db, c.id, worker_id="B")
    assert w1 is True
    assert w2 is False
    assert m2["claimed_by"] == "A"


def test_claim_lease_expiry_recovery(hard_db):
    c = _mk_campaign(hard_db, [1, 2])
    claim_campaign(hard_db, c.id, worker_id="dead", lease_minutes=30)
    c.claim_expires_at = datetime.utcnow() - timedelta(seconds=5)
    hard_db.commit()
    won, meta = claim_campaign(hard_db, c.id, worker_id="alive")
    assert won is True
    assert meta["claimed_by"] == "alive"


def test_campaign_scoped_auth_and_relock(hard_db, monkeypatch):
    monkeypatch.delenv("STORY_ACCOUNT_MUTATION_ALLOWLIST", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("STORY_MUTATIONS_ENABLED", "true")
    c = _mk_campaign(hard_db, [42, 43, 44])
    authorize_wave(hard_db, c.id, [42, 43], wave_index=0)
    from src.stories.mutation_boundary import story_account_mutation_allowed

    ok42, reason = story_account_mutation_allowed(42)
    assert ok42 and "campaign_wave" in reason
    ok44, _ = story_account_mutation_allowed(44)
    assert ok44 is False

    revoke_wave_authorization(hard_db, c.id)
    ok_after, _ = story_account_mutation_allowed(42)
    assert ok_after is False


def test_authorize_rejects_wave_over_max(hard_db):
    c = _mk_campaign(hard_db, list(range(1, 40)))
    with pytest.raises(ValueError, match="WAVE_SIZE_EXCEEDS_MAX"):
        authorize_wave(hard_db, c.id, list(range(1, 27)), wave_index=0)


def test_overlapping_campaign_account_lock(hard_db):
    c1 = _mk_campaign(hard_db, [7, 8])
    c2 = _mk_campaign(hard_db, [7, 9])
    authorize_wave(hard_db, c1.id, [7], wave_index=0)
    with pytest.raises(ValueError, match="account_locked_by_other_campaign"):
        authorize_wave(hard_db, c2.id, [7], wave_index=0)
    revoke_wave_authorization(hard_db, c1.id)
    authorize_wave(hard_db, c2.id, [7], wave_index=0)
    revoke_wave_authorization(hard_db, c2.id)


def test_progress_unique_and_no_resend(hard_db):
    c = _mk_campaign(hard_db, [5])
    ensure_progress_row(hard_db, campaign_id=c.id, wave_index=0, account_id=5, status="pending")
    update_progress(hard_db, campaign_id=c.id, wave_index=0, account_id=5, status="reconciled")
    update_progress(hard_db, campaign_id=c.id, wave_index=0, account_id=5, status="attempting")
    row = (
        hard_db.query(AutoStoryAccountProgress)
        .filter_by(campaign_id=c.id, wave_index=0, account_id=5)
        .one()
    )
    assert row.status == "reconciled"
    assert hard_db.query(AutoStoryAccountProgress).count() == 1


def test_plan_full_fleet_bounded_unique(hard_db):
    ids = list(range(1000, 1094))
    for i, aid in enumerate(ids):
        hard_db.add(
            Account(phone_number=f"+1555000{aid}", status="active")
        )
    hard_db.commit()
    plan = plan_full_fleet_waves(hard_db, ids)
    assert plan["wave_count"] == 4
    assert [w["size"] for w in plan["waves"]] == [25, 25, 25, 19]
    assert plan["duplicates"] == 0
    assert plan["any_over_max"] is False


def test_kill_switch_blocks_even_with_wave_auth(hard_db, monkeypatch):
    monkeypatch.setenv("STORY_MUTATIONS_ENABLED", "false")
    monkeypatch.delenv("STORY_ACCOUNT_MUTATION_ALLOWLIST", raising=False)
    c = _mk_campaign(hard_db, [99])
    authorize_wave(hard_db, c.id, [99], wave_index=0)
    from src.stories.mutation_boundary import StoryMutationService, StoryMutationTrigger

    decision = StoryMutationService.evaluate(
        account_id=99,
        trigger=StoryMutationTrigger.OPERATOR,
        scope="test",
    )
    assert decision.allowed is False
    assert decision.denial_reason == "story_mutations_disabled"
    revoke_wave_authorization(hard_db, c.id)


def test_pause_activate_cancel_are_state_only(hard_db, monkeypatch):
    from src.stories.auto_story_service import (
        activate_campaign,
        cancel_campaign,
        pause_campaign,
    )
    from src.stories.mutation_boundary import get_provider_call_count, reset_provider_call_counter

    # AutoStory retirement freeze (2026-09-03): activate_campaign now requires
    # explicit re-enablement in production; this test still exercises the
    # underlying state-machine behavior, which is unchanged.
    monkeypatch.setenv("AUTOSTORY_CAMPAIGN_CREATION_ENABLED", "true")

    @contextmanager
    def _ctx():
        try:
            yield hard_db
            hard_db.commit()
        except Exception:
            hard_db.rollback()
            raise

    monkeypatch.setattr("src.stories.auto_story_service.get_db_context", _ctx)
    monkeypatch.setattr(
        "src.stories.autostory_media.validate_campaign_media",
        lambda path: {"ok": True, "path": path, "error": None},
    )
    reset_provider_call_counter()
    c = _mk_campaign(hard_db, [1])
    paused = pause_campaign(int(c.id))
    assert paused["ok"] is True
    assert paused["campaign"]["status"] == "paused"
    resumed = activate_campaign(
        int(c.id),
        {
            "explicit_operator_approval": True,
            "confirmation_token": "I_CONFIRM_STORY_PUBLISH",
        },
    )
    assert resumed["ok"] is True
    assert resumed["campaign"]["status"] == "active"
    cancelled = cancel_campaign(int(c.id))
    assert cancelled["ok"] is True
    assert cancelled["campaign"]["status"] == "cancelled"
    assert get_provider_call_count() == 0


def test_legacy_stale_attempting_recovered_and_excluded_no_resend(hard_db, monkeypatch):
    """accounts_publish_once compatibility for the attempting-recovery fix.

    A stale attempting row (true-kill crash window, no exception ever ran)
    must resolve to ambiguous and then be permanently excluded from
    reselection -- exactly like any other PROGRESS_NO_RESEND status already
    is for legacy one-shot campaigns. _mk_campaign leaves campaign_mode at
    its accounts_publish_once column default, so this exercises the legacy
    branch of select_next_wave_accounts, not the recurring_daily path.
    """
    from src.stories.autostory_hardening import (
        progress_status,
        recover_stale_attempting,
        select_next_wave_accounts,
    )

    monkeypatch.setattr(
        "src.stories.autostory_hardening.is_account_certified_publish",
        lambda db, aid: (True, "certified_publish"),
    )
    db = hard_db
    acc = Account(phone_number="+15551234567")
    db.add(acc)
    db.commit()
    aid = int(acc.id)
    c = _mk_campaign(db, [aid])
    assert c.campaign_mode == "accounts_publish_once"

    update_progress(db, campaign_id=int(c.id), wave_index=0, account_id=aid, status="attempting")
    row = (
        db.query(AutoStoryAccountProgress)
        .filter_by(campaign_id=int(c.id), wave_index=0, account_id=aid)
        .one()
    )
    row.updated_at = datetime.utcnow() - timedelta(minutes=45)
    db.commit()

    # Too-fresh case first (age 0): not yet recovered, still owned by a
    # worker that could legitimately still be in flight.
    fresh_row_untouched = recover_stale_attempting(
        db, campaign_id=int(c.id), wave_index=0, account_id=aid, now=row.updated_at
    )
    assert fresh_row_untouched is None
    assert (
        progress_status(db, campaign_id=int(c.id), wave_index=0, account_id=aid) == "attempting"
    )

    sel = select_next_wave_accounts(db, c, wave_index=0)
    assert aid not in sel["wave_account_ids"]
    assert progress_status(db, campaign_id=int(c.id), wave_index=0, account_id=aid) == "ambiguous"

    # Idempotent and permanently excluded: legacy mode gives each account
    # exactly one terminal outcome per campaign.
    sel2 = select_next_wave_accounts(db, c, wave_index=1)
    assert aid not in sel2["wave_account_ids"]
