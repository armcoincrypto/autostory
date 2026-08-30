"""Wave G: Activity log humanization.

The dashboard's Activity modal previously dumped raw SystemLog event codes
(e.g. "autostory.account.deferred") verbatim. src/dashboard/templates/
stories.html now has a humanizeActivityEvent(ev) JS function that maps each
known event code + its details fields to a plain sentence (verified
directly against the real API/DB in a live browser session during
development; JS itself isn't unit-tested from pytest).

What *is* tested here, from Python, is the cross-boundary contract the JS
switch statement depends on: that get_campaign_activity() -- the API this
modal actually calls -- returns events whose `details` dict really does
carry the exact field names the humanizer reads for each event code. A
rename on the Python emitter side without a matching JS update would slip
past every existing test; these lock the contract.
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.database import Base
from src.stories.autostory_operator_preview import emit_autostory_system_log, get_campaign_activity

TEMPLATE = Path("src/dashboard/templates/stories.html").read_text(encoding="utf-8")

# Every event code the backend actually emits (see auto_story_service.py /
# autostory_hardening.py) must have a matching case in the JS humanizer.
KNOWN_EVENT_CODES = [
    "autostory.campaign.claimed",
    "autostory.campaign.started",
    "autostory.campaign.paused",
    "autostory.campaign.completed",
    "autostory.wave.started",
    "autostory.wave.completed",
    "autostory.account.started",
    "autostory.account.published",
    "autostory.account.failed",
    "autostory.account.deferred",
    "autostory.authorization.revoked",
]


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_humanizer_function_present_in_template() -> None:
    assert "function humanizeActivityEvent(ev)" in TEMPLATE


def test_activity_render_calls_humanizer_not_raw_event() -> None:
    # Regression guard: the raw `${ev.event}` dump must not have crept back in.
    assert "humanizeActivityEvent(ev)" in TEMPLATE
    assert "${t.slice(0, 19) || t} ${ev.event}$" not in TEMPLATE


def test_every_known_event_code_has_a_humanizer_case() -> None:
    for code in KNOWN_EVENT_CODES:
        assert f"case '{code}':" in TEMPLATE, f"no humanizer case for {code}"


def test_unmapped_event_falls_back_to_raw_code_not_hidden() -> None:
    # The default branch must still surface the raw event string somehow --
    # a future event type must never silently disappear from Activity.
    assert "default: {" in TEMPLATE
    assert "return `${ev.event}${waveSuf}${aidSuf}`;" in TEMPLATE


def test_deferred_event_details_carry_fields_humanizer_reads(db=None) -> None:
    db = _db()
    emit_autostory_system_log(
        db,
        "autostory.account.deferred",
        level="WARNING",
        account_id=141,
        campaign_id=7,
        wave_number=2,
        error="story_auth_blocked_until_future",
        reason="fresh_story_auth_failed",
        blocked_until="2026-08-31T10:00:00Z",
    )
    db.commit()
    events = get_campaign_activity(db, 7)
    assert len(events) == 1
    d = events[0]["details"]
    assert d["error"] == "story_auth_blocked_until_future"
    assert d["blocked_until"] == "2026-08-31T10:00:00Z"
    assert events[0]["account_id"] == 141


def test_wave_completed_event_details_carry_fields_humanizer_reads() -> None:
    db = _db()
    emit_autostory_system_log(
        db, "autostory.wave.completed", campaign_id=8, wave_number=3, wave_size=5, successful=3, failed=2,
    )
    db.commit()
    events = get_campaign_activity(db, 8)
    d = events[0]["details"]
    assert d["successful"] == 3
    assert d["failed"] == 2
    assert d["wave_number"] == 3


def test_account_published_event_details_carry_fields_humanizer_reads() -> None:
    db = _db()
    emit_autostory_system_log(
        db, "autostory.account.published", account_id=140, campaign_id=9,
        wave_number=1, telegram_story_id=4455,
    )
    db.commit()
    events = get_campaign_activity(db, 9)
    d = events[0]["details"]
    assert d["telegram_story_id"] == 4455
    assert events[0]["account_id"] == 140


def test_authorization_revoked_event_details_carry_fields_humanizer_reads() -> None:
    db = _db()
    emit_autostory_system_log(
        db, "autostory.authorization.revoked", campaign_id=10,
        active_authorizations=0, previously_authorized_count=5,
    )
    db.commit()
    events = get_campaign_activity(db, 10)
    d = events[0]["details"]
    assert d["previously_authorized_count"] == 5


def test_activity_scoped_to_its_own_campaign() -> None:
    db = _db()
    emit_autostory_system_log(db, "autostory.wave.started", campaign_id=11, wave_number=1, wave_size=1)
    emit_autostory_system_log(db, "autostory.wave.started", campaign_id=12, wave_number=1, wave_size=1)
    db.commit()
    events = get_campaign_activity(db, 11)
    assert len(events) == 1
    assert events[0]["details"]["campaign_id"] == 11
