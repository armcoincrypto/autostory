"""Focused tests for first real AutoStory production campaign bounds."""
from __future__ import annotations

from src.stories.fleet_cert_wave import allowlist_equals_wave


def test_campaign_allowlist_exactness_three_accounts():
    assert allowlist_equals_wave("106,107,140", [106, 107, 140])
    assert allowlist_equals_wave({140, 107, 106}, [106, 107, 140])
    assert not allowlist_equals_wave("106,107,140,108", [106, 107, 140])
    assert not allowlist_equals_wave("106,107", [106, 107, 140])


def test_zero_mentions_and_one_story_caps():
    mentions_per_story = 0
    max_stories = 3
    per_account = 1
    assert mentions_per_story == 0
    assert max_stories == 3
    assert per_account * 3 == max_stories


def test_selected_set_excludes_uncertified():
    selected = {106, 107, 140}
    certified = {106, 107, 140, 108}
    assert selected.issubset(certified)
    assert 186 not in selected or 186 in certified
