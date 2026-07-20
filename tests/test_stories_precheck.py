"""Story precheck helper tests for dry-run semantics."""
from __future__ import annotations

from src.core.database import get_db_context
from src.stories.rotation_audit import media_precheck, select_mention_candidates, story_purpose_compatible


def test_story_purpose_compatibility_includes_both() -> None:
    assert story_purpose_compatible("both")
    assert story_purpose_compatible("autostory")
    assert not story_purpose_compatible("disabled")


def test_media_precheck_requires_existing_media() -> None:
    missing = media_precheck("")
    assert missing["ok"] is False
    assert missing["required"] is True


def test_random_mention_selection_is_deduped_and_read_only() -> None:
    with get_db_context() as db:
        selected = select_mention_candidates(
            db,
            source_chat_id=None,
            count=10,
            strategy="random",
            mutate=False,
        )

    user_ids = [row["user_id"] for row in selected]
    assert len(user_ids) == len(set(user_ids))
    assert len(selected) <= 10
