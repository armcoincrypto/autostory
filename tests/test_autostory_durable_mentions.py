"""Wave D: durable random-mention target selection (no Telegram, no production).

Covers: 0/1/2/3 mentions_per_story allocation sizing, no-duplicate-within-wave,
self-mention exclusion, invalid/unresolvable target handling at publish time,
same-slot durability (retry reuses the first selection, never re-randomizes),
persist-never-overwrites, and multi-account/multi-day independence.
AUTOSTORY_MENTIONS_PRODUCTION_CERTIFIED stays false throughout -- these tests
exercise the local durability/selection machinery directly, not the flag.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.database import Base
from src.core.models import Account, AccountStatus, AutoStoryAccountProgress, DiscoveredUser
from src.stories.autostory_hardening import (
    load_durable_mention_plan,
    persist_mention_plan_for_wave,
)
from src.stories.rotation_audit import (
    allocate_mentions_without_replacement,
    select_mention_candidates,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _seed_account(db, *, account_id: int, user_id: int | None = None) -> Account:
    a = Account(
        id=account_id,
        phone_number=f"+1555200{account_id:04d}",
        status=AccountStatus.ACTIVE,
        user_id=user_id,
    )
    db.add(a)
    db.commit()
    return a


def _seed_discovered_users(
    db, *, source_chat_id: int, n: int, start_user_id: int = 9_000_000, times_mentioned: int = 0
) -> list[int]:
    ids = []
    for i in range(n):
        uid = start_user_id + i
        db.add(
            DiscoveredUser(
                user_id=uid,
                username=f"seed_user_{uid}",
                source_chat_id=source_chat_id,
                source_chat_title="seed_chat",
                discovered_at=datetime.utcnow(),
                times_mentioned=times_mentioned,
                is_blocked=False,
            )
        )
        ids.append(uid)
    db.commit()
    return ids


SOURCE_CHAT = 1936532075


# ---------------------------------------------------------------------------
# 0/1/2/3 mentions_per_story allocation sizing
# ---------------------------------------------------------------------------


def test_zero_mentions_returns_empty_candidates(db) -> None:
    _seed_discovered_users(db, source_chat_id=SOURCE_CHAT, n=10)
    candidates = select_mention_candidates(db, source_chat_id=SOURCE_CHAT, count=0)
    assert candidates == []


@pytest.mark.parametrize("mentions_per_story", [1, 2, 3])
def test_allocate_gives_each_account_exactly_n_mentions(db, mentions_per_story) -> None:
    account_ids = [201, 202, 203]
    _seed_discovered_users(db, source_chat_id=SOURCE_CHAT, n=mentions_per_story * len(account_ids))
    candidates = select_mention_candidates(
        db, source_chat_id=SOURCE_CHAT, count=mentions_per_story * len(account_ids)
    )
    allocated = allocate_mentions_without_replacement(
        candidates, account_ids=account_ids, mentions_per_story=mentions_per_story
    )
    assert len(allocated) == len(account_ids)
    for chunk in allocated:
        assert len(chunk) == mentions_per_story


def test_zero_mentions_allocation_is_empty_lists_per_account(db) -> None:
    account_ids = [301, 302]
    allocated = allocate_mentions_without_replacement(
        [], account_ids=account_ids, mentions_per_story=0
    )
    assert allocated == [[], []]


# ---------------------------------------------------------------------------
# No duplicate targets within one wave, across accounts
# ---------------------------------------------------------------------------


def test_no_duplicate_targets_across_accounts_in_same_wave(db) -> None:
    account_ids = [401, 402, 403]
    mentions_per_story = 2
    _seed_discovered_users(
        db, source_chat_id=SOURCE_CHAT, n=mentions_per_story * len(account_ids)
    )
    candidates = select_mention_candidates(
        db, source_chat_id=SOURCE_CHAT, count=mentions_per_story * len(account_ids)
    )
    allocated = allocate_mentions_without_replacement(
        candidates, account_ids=account_ids, mentions_per_story=mentions_per_story
    )
    seen: set[int] = set()
    for chunk in allocated:
        for cand in chunk:
            assert cand["user_id"] not in seen, "same user mentioned twice within one wave"
            seen.add(cand["user_id"])
    assert len(seen) == mentions_per_story * len(account_ids)


def test_pool_smaller_than_requested_never_duplicates(db) -> None:
    """Fewer discovered users than requested -- allocation must still be duplicate-free."""
    account_ids = [501, 502, 503]
    mentions_per_story = 3
    # Only 4 users exist; 3 accounts x 3 mentions = 9 requested.
    _seed_discovered_users(db, source_chat_id=SOURCE_CHAT, n=4)
    candidates = select_mention_candidates(
        db, source_chat_id=SOURCE_CHAT, count=mentions_per_story * len(account_ids)
    )
    assert len(candidates) == 4
    allocated = allocate_mentions_without_replacement(
        candidates, account_ids=account_ids, mentions_per_story=mentions_per_story
    )
    seen: set[int] = set()
    for chunk in allocated:
        for cand in chunk:
            assert cand["user_id"] not in seen
            seen.add(cand["user_id"])
    # Total applied never exceeds what was actually available.
    assert len(seen) == 4


# ---------------------------------------------------------------------------
# Self-mention exclusion
# ---------------------------------------------------------------------------


def test_self_mention_excluded_from_candidate_pool(db) -> None:
    publishing_account_user_id = 9_000_000  # collides with first seeded discovered user
    _seed_discovered_users(db, source_chat_id=SOURCE_CHAT, n=5, start_user_id=9_000_000)
    candidates = select_mention_candidates(
        db,
        source_chat_id=SOURCE_CHAT,
        count=5,
        exclude_user_ids={publishing_account_user_id},
    )
    assert all(c["user_id"] != publishing_account_user_id for c in candidates)
    assert len(candidates) == 4  # pool had 5, one excluded


def test_no_exclusion_when_publishing_account_not_in_pool(db) -> None:
    _seed_discovered_users(db, source_chat_id=SOURCE_CHAT, n=3, start_user_id=9_100_000)
    candidates = select_mention_candidates(
        db, source_chat_id=SOURCE_CHAT, count=3, exclude_user_ids={12345}
    )
    assert len(candidates) == 3


# ---------------------------------------------------------------------------
# Duplicate discovered-user / staleness semantics (times_mentioned filter)
# ---------------------------------------------------------------------------


def test_already_mentioned_users_excluded_by_default(db) -> None:
    _seed_discovered_users(
        db, source_chat_id=SOURCE_CHAT, n=3, start_user_id=9_200_000, times_mentioned=1
    )
    _seed_discovered_users(
        db, source_chat_id=SOURCE_CHAT, n=2, start_user_id=9_300_000, times_mentioned=0
    )
    candidates = select_mention_candidates(db, source_chat_id=SOURCE_CHAT, count=10)
    assert len(candidates) == 2
    assert all(c["user_id"] >= 9_300_000 for c in candidates)


def test_blocked_users_excluded(db) -> None:
    db.add(
        DiscoveredUser(
            user_id=9_400_001,
            username="blocked_user",
            source_chat_id=SOURCE_CHAT,
            discovered_at=datetime.utcnow(),
            times_mentioned=0,
            is_blocked=True,
        )
    )
    db.commit()
    candidates = select_mention_candidates(db, source_chat_id=SOURCE_CHAT, count=10)
    assert candidates == []


# ---------------------------------------------------------------------------
# Invalid / deleted / unresolvable target handling at publish time
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unresolvable_mention_candidate_is_skipped_not_applied() -> None:
    from src.stories.publisher import StoryPublisher

    pub = StoryPublisher()
    fake_client = MagicMock()
    fake_client.get_entity = AsyncMock(side_effect=ValueError("Cannot find any entity"))

    resolved = await pub._resolve_mention_candidate(
        fake_client, {"user_id": 555, "username": "deleted_user"}
    )
    assert resolved["ok"] is False
    assert resolved["reason"] == "story_mention_peer_unresolvable"


@pytest.mark.asyncio
async def test_resolvable_mention_candidate_is_applied() -> None:
    from src.stories.publisher import StoryPublisher

    pub = StoryPublisher()
    fake_entity = MagicMock()
    fake_entity.id = 777
    fake_entity.username = "alive_user"
    fake_client = MagicMock()
    fake_client.get_entity = AsyncMock(return_value=fake_entity)

    resolved = await pub._resolve_mention_candidate(
        fake_client, {"user_id": 777, "username": "alive_user"}
    )
    assert resolved["ok"] is True
    assert resolved["applied"] == {"user_id": 777, "username": "alive_user"}


@pytest.mark.asyncio
async def test_missing_username_and_user_id_is_rejected_without_telegram_call() -> None:
    from src.stories.publisher import StoryPublisher

    pub = StoryPublisher()
    fake_client = MagicMock()
    fake_client.get_entity = AsyncMock()

    resolved = await pub._resolve_mention_candidate(fake_client, {})
    assert resolved["ok"] is False
    fake_client.get_entity.assert_not_called()


# ---------------------------------------------------------------------------
# Same durable slot -> same mention targets (the core Wave B invariant)
# ---------------------------------------------------------------------------


def test_no_durable_plan_yet_returns_none(db) -> None:
    _seed_account(db, account_id=601)
    result = load_durable_mention_plan(
        db, campaign_id=1, wave_index=0, account_ids=[601]
    )
    assert result is None


def test_persist_then_load_round_trips_exact_targets(db) -> None:
    _seed_account(db, account_id=602)
    chunk = [{"user_id": 111, "username": "a"}, {"user_id": 222, "username": "b"}]
    persist_mention_plan_for_wave(
        db, campaign_id=5, wave_index=0, per_account_plan={602: chunk}
    )
    loaded = load_durable_mention_plan(db, campaign_id=5, wave_index=0, account_ids=[602])
    assert loaded == {602: chunk}


def test_empty_plan_is_durable_and_reused_not_reselected(db) -> None:
    """mentions_per_story == 0 still counts as a decided slot."""
    _seed_account(db, account_id=603)
    persist_mention_plan_for_wave(
        db, campaign_id=6, wave_index=0, per_account_plan={603: []}
    )
    loaded = load_durable_mention_plan(db, campaign_id=6, wave_index=0, account_ids=[603])
    assert loaded == {603: []}


def test_retry_after_crash_reuses_first_selection_exactly(db) -> None:
    """Simulates: wave selects+persists, then a crash/retry re-enters the same
    slot. The retry must load the identical targets rather than drawing a new
    random sample."""
    account_ids = [701, 702]
    mentions_per_story = 2
    _seed_discovered_users(db, source_chat_id=SOURCE_CHAT, n=mentions_per_story * len(account_ids))

    # First attempt: no durable plan yet -> select fresh, then persist.
    assert load_durable_mention_plan(db, campaign_id=9, wave_index=0, account_ids=account_ids) is None
    candidates = select_mention_candidates(
        db, source_chat_id=SOURCE_CHAT, count=mentions_per_story * len(account_ids)
    )
    allocated = allocate_mentions_without_replacement(
        candidates, account_ids=account_ids, mentions_per_story=mentions_per_story
    )
    first_plan = dict(zip(account_ids, allocated))
    persist_mention_plan_for_wave(db, campaign_id=9, wave_index=0, per_account_plan=first_plan)

    # Simulated crash/retry: re-enter the same (campaign, wave) slot.
    retry_plan = load_durable_mention_plan(db, campaign_id=9, wave_index=0, account_ids=account_ids)
    assert retry_plan is not None
    assert retry_plan == first_plan


def test_persist_never_overwrites_an_already_decided_slot(db) -> None:
    _seed_account(db, account_id=801)
    first = [{"user_id": 1, "username": "first"}]
    second = [{"user_id": 2, "username": "second"}]
    persist_mention_plan_for_wave(db, campaign_id=10, wave_index=0, per_account_plan={801: first})
    persist_mention_plan_for_wave(db, campaign_id=10, wave_index=0, per_account_plan={801: second})
    loaded = load_durable_mention_plan(db, campaign_id=10, wave_index=0, account_ids=[801])
    assert loaded == {801: first}


def test_partial_account_set_without_durable_plan_returns_none(db) -> None:
    """If even one requested account lacks a durable plan for this slot, the
    whole lookup returns None (caller must select fresh for all of them) --
    it must never silently reuse a plan for only part of the wave."""
    _seed_account(db, account_id=901)
    _seed_account(db, account_id=902)
    persist_mention_plan_for_wave(
        db, campaign_id=11, wave_index=0, per_account_plan={901: [{"user_id": 1, "username": "x"}]}
    )
    result = load_durable_mention_plan(db, campaign_id=11, wave_index=0, account_ids=[901, 902])
    assert result is None
    # The already-decided account's row is untouched.
    row = (
        db.query(AutoStoryAccountProgress)
        .filter_by(campaign_id=11, wave_index=0, account_id=901)
        .one()
    )
    assert row.mention_plan == [{"user_id": 1, "username": "x"}]


# ---------------------------------------------------------------------------
# Multi-account / multi-day independence
# ---------------------------------------------------------------------------


def test_different_wave_indices_get_independent_durable_plans(db) -> None:
    """Day 1 (wave_index=0) and Day 2 (wave_index=1) for the same account/campaign
    are independent slots -- persisting one must not satisfy a lookup for the
    other."""
    _seed_account(db, account_id=1001)
    persist_mention_plan_for_wave(
        db, campaign_id=20, wave_index=0, per_account_plan={1001: [{"user_id": 1, "username": "d1"}]}
    )
    day2 = load_durable_mention_plan(db, campaign_id=20, wave_index=1, account_ids=[1001])
    assert day2 is None

    persist_mention_plan_for_wave(
        db, campaign_id=20, wave_index=1, per_account_plan={1001: [{"user_id": 2, "username": "d2"}]}
    )
    day1 = load_durable_mention_plan(db, campaign_id=20, wave_index=0, account_ids=[1001])
    day2 = load_durable_mention_plan(db, campaign_id=20, wave_index=1, account_ids=[1001])
    assert day1 == {1001: [{"user_id": 1, "username": "d1"}]}
    assert day2 == {1001: [{"user_id": 2, "username": "d2"}]}


def test_different_campaigns_get_independent_durable_plans(db) -> None:
    _seed_account(db, account_id=1101)
    persist_mention_plan_for_wave(
        db, campaign_id=30, wave_index=0, per_account_plan={1101: [{"user_id": 1, "username": "c30"}]}
    )
    other_campaign = load_durable_mention_plan(db, campaign_id=31, wave_index=0, account_ids=[1101])
    assert other_campaign is None


def test_multi_account_multi_day_grid_is_fully_independent_and_durable(db) -> None:
    account_ids = [1201, 1202, 1203]
    for aid in account_ids:
        _seed_account(db, account_id=aid)

    grid: dict[tuple[int, int], dict[int, list[dict]]] = {}
    for wave_index in (0, 1, 2):
        per_account = {
            aid: [{"user_id": 10_000 + wave_index * 100 + aid, "username": f"u{wave_index}_{aid}"}]
            for aid in account_ids
        }
        persist_mention_plan_for_wave(
            db, campaign_id=40, wave_index=wave_index, per_account_plan=per_account
        )
        grid[(40, wave_index)] = per_account

    # Every (campaign, wave) slot reloads exactly what was persisted, and only that slot.
    for wave_index in (0, 1, 2):
        loaded = load_durable_mention_plan(
            db, campaign_id=40, wave_index=wave_index, account_ids=account_ids
        )
        assert loaded == grid[(40, wave_index)]

    # Cross-check: no two waves share a target (independent random draws in this grid).
    all_targets = [
        cand["user_id"]
        for per_account in grid.values()
        for chunk in per_account.values()
        for cand in chunk
    ]
    assert len(all_targets) == len(set(all_targets))
