"""Tests for fleet production-certification wave selection and allowlist exactness."""
from __future__ import annotations

import pytest

from src.stories.fleet_cert_wave import (
    allowlist_equals_wave,
    select_certification_wave,
)


def _ready(aid: int, **over):
    row = {
        "account_id": aid,
        "classification": "READY_FOR_SEPARATE_CONTROLLED_CANARY",
        "auth_valid": True,
        "identity_matches": True,
        "story_api_available": True,
        "story_probe_status": "allowed",
        "session_readable": True,
        "stories_today_effective": 0,
        "daily_limit": 1,
        "display_name": f"A{aid}",
        "telegram_user_id": 1000 + aid,
    }
    row.update(over)
    return row


def test_wave_selects_five_oldest_ready_after_safety():
    accounts = [_ready(i) for i in range(100, 120)]
    # inject already certified + disabled
    accounts.append(_ready(106, classification="CERTIFIED_PUBLISH"))
    accounts.append(_ready(200, classification="ACCOUNT_DISABLED"))
    wave = select_certification_wave(accounts, wave_size=5, exclude_ids={106, 107, 140})
    assert [w["account_id"] for w in wave] == [100, 101, 102, 103, 104]


def test_excludes_certified_disabled_and_non_ready():
    accounts = [
        _ready(106, classification="CERTIFIED_PUBLISH"),
        _ready(108),
        _ready(109, classification="ACCOUNT_DISABLED"),
        _ready(111, classification="INTENTIONALLY_EXCLUDED"),
        _ready(112),
        _ready(114, classification="AUTH_FAILED"),
        _ready(115),
    ]
    wave = select_certification_wave(accounts, wave_size=5, exclude_ids={106})
    assert [w["account_id"] for w in wave] == [108, 112, 115]


def test_rejects_no_capacity_and_auth_failures():
    accounts = [
        _ready(108, stories_today_effective=1),
        _ready(109, auth_valid=False),
        _ready(111, identity_matches=False),
        _ready(112),
        _ready(114),
    ]
    wave = select_certification_wave(accounts, wave_size=5, exclude_ids=set())
    assert [w["account_id"] for w in wave] == [112, 114]


def test_wave_size_cap():
    accounts = [_ready(i) for i in range(200, 220)]
    with pytest.raises(ValueError):
        select_certification_wave(accounts, wave_size=11)
    wave = select_certification_wave(accounts, wave_size=10, exclude_ids=set())
    assert len(wave) == 10


def test_allowlist_equals_wave_exactness():
    assert allowlist_equals_wave("108,109,111,112,114", [108, 109, 111, 112, 114])
    assert not allowlist_equals_wave("108,109,111,112,114,106", [108, 109, 111, 112, 114])
    assert not allowlist_equals_wave("108,109", [108, 109, 111])
    assert allowlist_equals_wave({114, 112, 111, 109, 108}, [108, 109, 111, 112, 114])


def test_zero_mentions_policy_constant():
    # Certification canaries must use mentions=0; document via selection payload contract.
    wave = select_certification_wave([_ready(108)], wave_size=1, exclude_ids=set())
    assert wave[0]["account_id"] == 108
    # callers must force mentions_per_story=0 — enforced in orchestration, not selection
