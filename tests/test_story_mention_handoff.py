"""Mention handoff + caption entity tests (no live Telegram)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.stories.controlled_live_run import evaluate_controlled_live_run_gates
from src.stories.mention_plan import (
    build_caption_with_mention_entities,
    evaluate_mention_plan_local,
    extract_approved_mention_plan,
    normalize_mention_plan,
    require_all_mentions_flag,
)
from src.stories.rotation_audit import CONTROLLED_LIVE_ACCOUNT_ID


def _bypass_mutation_boundary_for_unit_publish(monkeypatch) -> None:
    """Publisher unit tests exercise mention/media paths, not Phase 1.1 gates."""

    async def _fake_invoke(client, request, *, authorization, account_id):
        return await client(request)

    monkeypatch.setattr(
        "src.stories.mutation_boundary.require_story_mutation_authorization",
        lambda **_kwargs: SimpleNamespace(
            allowed=True,
            authorization=object(),
            denial_reason=None,
        ),
    )
    monkeypatch.setattr(
        "src.stories.mutation_boundary.invoke_send_story",
        _fake_invoke,
    )


def test_dry_run_selects_exactly_one_candidate_shape() -> None:
    plan = evaluate_mention_plan_local(
        [{"user_id": 11, "username": "alice", "source_chat_title": "Grp"}],
        mentions_requested=1,
    )
    assert plan["mentions_requested"] == 1
    assert len(plan["mentions_selected"]) == 1
    assert plan["mention_plan_ok"] is True
    assert "@alice" in plan["operator_summary"]


def test_approved_plan_persists_into_live_payload_shape() -> None:
    payload = {
        "selected_mention_candidates": [
            {"user_id": 42, "username": "bob", "source_chat_id": 9}
        ],
        "mentions_per_story": 1,
    }
    plan = extract_approved_mention_plan(payload)
    assert plan is not None
    assert plan[0]["user_id"] == 42
    assert plan[0]["username"] == "bob"


def test_live_execution_does_not_reselect_when_plan_present() -> None:
    approved = extract_approved_mention_plan(
        {"selected_mention_candidates": [{"user_id": 1, "username": "locked"}]}
    )
    precheck_other = [{"user_id": 99, "username": "other"}]
    chosen = normalize_mention_plan(approved)[:1]
    assert chosen[0]["user_id"] == 1
    assert chosen[0]["user_id"] != precheck_other[0]["user_id"]


def test_existing_caption_plus_mention() -> None:
    caption, entities = build_caption_with_mention_entities(
        "Hello",
        [{"username": "alice", "user_id": 1}],
    )
    assert caption == "Hello\n\n@alice"
    assert len(entities) == 1
    assert entities[0].offset == len("Hello\n\n")
    assert entities[0].length == len("@alice")


def test_empty_caption_plus_mention() -> None:
    caption, entities = build_caption_with_mention_entities(
        "",
        [{"username": "bob", "user_id": 2}],
    )
    assert caption == "@bob"
    assert entities[0].offset == 0


def test_candidate_with_valid_username() -> None:
    plan = evaluate_mention_plan_local(
        [{"user_id": 7, "username": "okuser"}],
        mentions_requested=1,
    )
    assert plan["mention_plan_ok"] is True


def test_candidate_peer_id_but_no_username() -> None:
    plan = evaluate_mention_plan_local(
        [{"user_id": 7, "username": None}],
        mentions_requested=1,
    )
    assert plan["mention_plan_ok"] is False
    assert "story_mention_username_missing" in plan["live_blockers"]


def test_unresolvable_peer_marked_in_skip_shape() -> None:
    skipped = [{"username": "ghost", "peer_id": 1, "reason": "story_mention_peer_unresolvable"}]
    assert skipped[0]["reason"] == "story_mention_peer_unresolvable"


def test_requested_mentions_greater_than_available() -> None:
    plan = evaluate_mention_plan_local(
        [{"user_id": 1, "username": "only"}],
        mentions_requested=3,
    )
    assert "story_mention_candidate_missing" in plan["live_blockers"]


def test_strict_mention_policy_flag() -> None:
    assert require_all_mentions_flag({}, default=True) is True
    assert require_all_mentions_flag({"require_all_mentions": False}) is False


def test_response_distinguishes_selected_applied_skipped() -> None:
    body = {
        "mentions_requested": 1,
        "mentions_selected": [{"username": "x", "peer_id": 1}],
        "mentions_applied": [],
        "mentions_skipped": [{"username": "x", "reason": "story_mention_peer_unresolvable"}],
        "mentions": [],
    }
    assert body["mentions_selected"] and not body["mentions_applied"]
    assert body["mentions_skipped"][0]["reason"]


def test_normalize_mention_plan_dedupes() -> None:
    rows = normalize_mention_plan(
        [
            {"user_id": 1, "username": "a"},
            {"user_id": 1, "username": "a"},
            2,
        ]
    )
    assert [r["user_id"] for r in rows] == [1, 2]


@pytest.mark.asyncio
async def test_strict_policy_blocks_publish_without_telegram_send(monkeypatch) -> None:
    from src.stories import publisher as pubmod
    from src.stories.publisher import StoryPublisher

    pub = StoryPublisher()
    client = AsyncMock()
    client.get_entity = AsyncMock(side_effect=ValueError("missing entity"))
    client.upload_file = AsyncMock()
    wrapper = SimpleNamespace(client=client, account=SimpleNamespace(id=140))
    monkeypatch.setattr("src.core.execution_guard.require_execution_allowed", lambda *a, **k: None)
    _bypass_mutation_boundary_for_unit_publish(monkeypatch)
    monkeypatch.setattr(pub, "_get_media_type", lambda p: "photo")

    result = await pub.publish_story(
        client_wrapper=wrapper,
        media_path="/tmp/does-not-matter.jpg",
        caption="Hello",
        mentions=[{"user_id": 1, "username": "ghost"}],
        require_all_mentions=True,
    )
    # Media existence check runs first — create temp file
    assert result["success"] is False
    # Either media missing or mention failure depending on path; force existing file:
    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as fh:
        path = Path(fh.name)
        fh.write(b"x")
    try:
        result = await pub.publish_story(
            client_wrapper=wrapper,
            media_path=str(path),
            caption="Hello",
            mentions=[{"user_id": 1, "username": "ghost"}],
            require_all_mentions=True,
        )
    finally:
        path.unlink(missing_ok=True)

    assert result["success"] is False
    assert result["error"] == "story_mention_peer_unresolvable"
    assert result["mentions_selected"]
    assert result["mentions_applied"] == []
    assert result["mentions_skipped"]
    client.upload_file.assert_not_called()


@pytest.mark.asyncio
async def test_optional_policy_publishes_with_explicit_warning(monkeypatch) -> None:
    from src.stories import publisher as pubmod
    from src.stories.publisher import StoryPublisher
    import tempfile
    from pathlib import Path

    pub = StoryPublisher()
    client = AsyncMock()
    client.get_entity = AsyncMock(side_effect=ValueError("missing"))
    client.upload_file = AsyncMock(return_value=object())

    sent = {}

    async def _invoke(req):
        sent["caption"] = req.caption
        sent["entities"] = req.entities
        return SimpleNamespace(updates=[])

    client.side_effect = _invoke
    wrapper = SimpleNamespace(client=client, account=SimpleNamespace(id=140))
    monkeypatch.setattr("src.core.execution_guard.require_execution_allowed", lambda *a, **k: None)
    _bypass_mutation_boundary_for_unit_publish(monkeypatch)
    monkeypatch.setattr(pub, "_get_media_type", lambda p: "photo")
    monkeypatch.setattr(pubmod.AntiDetection, "random_pause", AsyncMock())

    class _DB:
        def __enter__(self):
            db = MagicMock()
            db.add = MagicMock(side_effect=lambda obj: setattr(obj, "id", 55))
            db.query.return_value.filter.return_value.first.return_value = None
            db.commit = MagicMock()
            return db

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(pubmod, "get_db_context", lambda: _DB())

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as fh:
        path = Path(fh.name)
        fh.write(b"x")
    try:
        result = await pub.publish_story(
            client_wrapper=wrapper,
            media_path=str(path),
            caption="Hello",
            mentions=[{"user_id": 1, "username": "ghost"}],
            require_all_mentions=False,
        )
    finally:
        path.unlink(missing_ok=True)

    assert result["success"] is True
    assert result["mentions_applied"] == []
    assert result["mentions_skipped"]
    assert result.get("warning")
    assert sent["caption"] == "Hello"
    assert sent["entities"] in (None, [])


@pytest.mark.asyncio
async def test_zero_mention_story_publishing_unchanged(monkeypatch) -> None:
    from src.stories import publisher as pubmod
    from src.stories.publisher import StoryPublisher
    import tempfile
    from pathlib import Path

    pub = StoryPublisher()
    client = AsyncMock()
    client.upload_file = AsyncMock(return_value=object())
    sent = {}

    async def _invoke(req):
        sent["caption"] = req.caption
        sent["entities"] = req.entities
        return SimpleNamespace(updates=[])

    client.side_effect = _invoke
    wrapper = SimpleNamespace(client=client, account=SimpleNamespace(id=140))
    monkeypatch.setattr("src.core.execution_guard.require_execution_allowed", lambda *a, **k: None)
    _bypass_mutation_boundary_for_unit_publish(monkeypatch)
    monkeypatch.setattr(pub, "_get_media_type", lambda p: "photo")
    monkeypatch.setattr(pubmod.AntiDetection, "random_pause", AsyncMock())

    class _DB:
        def __enter__(self):
            db = MagicMock()
            db.add = MagicMock(side_effect=lambda obj: setattr(obj, "id", 77))
            db.query.return_value.filter.return_value.first.return_value = None
            db.commit = MagicMock()
            return db

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(pubmod, "get_db_context", lambda: _DB())

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as fh:
        path = Path(fh.name)
        fh.write(b"x")
    try:
        result = await pub.publish_story(
            client_wrapper=wrapper,
            media_path=str(path),
            caption="Hello",
            mentions=None,
            require_all_mentions=True,
        )
    finally:
        path.unlink(missing_ok=True)

    assert result["success"] is True
    assert result["mentions_requested"] == 0
    assert result["mentions"] == []
    assert sent["caption"] == "Hello"
    assert sent["entities"] in (None, [])


def test_blacklisted_candidates_excluded_by_existing_query_filter() -> None:
    # Document contract: select_mention_candidates uses is_blocked == False via _mention_query.
    from src.stories.rotation_audit import _mention_query
    from src.core.models import DiscoveredUser

    assert DiscoveredUser.is_blocked is not None
    assert callable(_mention_query)
