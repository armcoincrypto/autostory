"""Wave 4: the Stories/account/day picker must not present 2 and 3 as normal
equivalent choices to 1. Verified product truth (two real production
canaries -- Campaign #18 STORIES_TOO_MUCH, Campaign #20 STORY_SEND_FLOOD_WEEKLY):
1/day is production-certified, 2/day is Telegram-capability dependent (not
guaranteed), 3/day is not certified. Source support for 2 and 3 stays intact --
this is a labeling/copy change only.
"""
from __future__ import annotations

from pathlib import Path

TEMPLATE = Path("src/dashboard/templates/stories.html").read_text(encoding="utf-8")


def test_option_labels_are_honest_about_capability() -> None:
    assert '<option value="1" selected>1 — Production certified</option>' in TEMPLATE
    assert '<option value="2">2 — Capability dependent</option>' in TEMPLATE
    assert '<option value="3">3 — Not certified</option>' in TEMPLATE
    # The old wording implied 2 and 3 were merely "not yet" tested, i.e. would
    # eventually just work -- that is no longer true and must not reappear.
    assert "Not yet certified" not in TEMPLATE


def test_default_selection_is_1() -> None:
    assert '<option value="1" selected>' in TEMPLATE


def test_helper_copy_never_promises_guaranteed_2_per_day() -> None:
    assert "Telegram may restrict some accounts" in TEMPLATE
    assert "AutoStory will never force publication beyond Telegram" in TEMPLATE
    helper_text = TEMPLATE.split('id="auto-spad"')[1][:600]
    assert "not guaranteed" in helper_text
    assert "2/day is guaranteed" not in helper_text
    assert "guaranteed 2" not in helper_text


def test_source_support_for_2_and_3_not_removed() -> None:
    # Copy-only change: the underlying architecture must still offer 2 and 3.
    assert '<option value="2">' in TEMPLATE
    assert '<option value="3">' in TEMPLATE
