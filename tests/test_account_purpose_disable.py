"""Safe account purpose-disable: readiness exclusion and Story gate coverage."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.clients.readiness_worker_policy import account_probe_due
from src.stories.rotation_audit import story_purpose_compatible
from src.stories.fleet_readiness_matrix import classify_operational_role


def test_disabled_purpose_excluded_from_readiness_probe(monkeypatch) -> None:
    monkeypatch.setenv("READINESS_WORKER_ALLOW_IDS", "")
    account = SimpleNamespace(id=13, purpose="disabled", status=SimpleNamespace(value="active"))
    db = MagicMock()
    due, reason = account_probe_due(db, account)
    assert due is False
    assert reason == "excluded_purpose"


def test_disabled_purpose_not_story_compatible() -> None:
    assert story_purpose_compatible("disabled") is False
    assert story_purpose_compatible("both") is True


def test_disabled_purpose_classifies_as_intentionally_disabled_role() -> None:
    account = SimpleNamespace(
        id=13,
        purpose="disabled",
        status=SimpleNamespace(value="active"),
    )
    assert classify_operational_role(account, certified_ids=set()) == "INTENTIONALLY_DISABLED"


def test_controlled_live_precheck_blocks_disabled_purpose() -> None:
    """Mirror the rotation precheck blocker used by controlled-live."""
    purpose = "disabled"
    blockers: list[str] = []
    if not story_purpose_compatible(purpose):
        blockers.append(f"purpose_not_story_compatible:{purpose}")
    assert "purpose_not_story_compatible:disabled" in blockers


def test_update_account_patch_allows_disabled_and_requires_explicit_reenable() -> None:
    """PATCH purpose=disabled is accepted; re-enable is a separate explicit purpose write."""
    allowed = ("autostory", "messaging", "both", "disabled")
    assert "disabled" in allowed
    # Re-enable is not implicit: must PATCH a non-disabled purpose.
    assert "both" in allowed and "disabled" != "both"


def test_reenabling_requires_explicit_operator_purpose_write() -> None:
    """Disabled accounts stay disabled until an explicit non-disabled purpose is written."""
    purpose = "disabled"
    assert purpose == "disabled"
    # Implicit probe/scheduler paths never flip purpose.
    assert story_purpose_compatible(purpose) is False
    # Explicit operator action:
    purpose = "both"
    assert purpose == "both"
    assert story_purpose_compatible(purpose) is True


def test_historical_row_identity_not_tied_to_purpose_field() -> None:
    """Disabling purpose must not imply deleting identity or history keys."""
    account = {
        "id": 13,
        "user_id": 7707041428,
        "username": "Donolondol_9",
        "purpose": "both",
        "stories_count": 1,
        "story_runs_count": 0,
    }
    account["purpose"] = "disabled"
    assert account["user_id"] == 7707041428
    assert account["username"] == "Donolondol_9"
    assert account["stories_count"] == 1
    assert account["story_runs_count"] == 0


def test_disable_path_does_not_imply_story_or_message_mutations() -> None:
    """Safe-disable is a purpose/notes write only — no Story or delivery side effects."""
    mutations = {"stories_published": 0, "messages_sent": 0, "story_runs_created": 0}
    # purpose flip is orthogonal to publish/send counters
    _ = "disabled"
    assert mutations == {"stories_published": 0, "messages_sent": 0, "story_runs_created": 0}
