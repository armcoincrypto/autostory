"""Durable certification must recognize multi-account controlled-run logs."""
from __future__ import annotations

from src.stories.fleet_certification import durable_certification_evidence


class _FakeStory:
    def __init__(self, id: int, account_id: int, story_id: int):
        self.id = id
        self.account_id = account_id
        self.story_id = story_id
        self.published_at = None


class _FakeLog:
    def __init__(self, id: int, details: dict):
        self.id = id
        self.details = details
        self.component = "controlled_live_story_run"


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, logs, stories):
        self._logs = logs
        self._stories = {s.id: s for s in stories}

    def query(self, model):
        return _FakeQuery(self._logs)

    def get(self, model, pk):
        return self._stories.get(int(pk))


def test_durable_certification_reads_v2_multi_steps():
    story = _FakeStory(id=99, account_id=107, story_id=7)
    log = _FakeLog(
        500,
        {
            "schema": "controlled_live_story_run_v2_multi",
            "steps": [
                {"ok": True, "account_id": 107, "db_id": 99, "story_id": 7},
                {"ok": False, "account_id": 108, "db_id": None, "story_id": None},
            ],
        },
    )
    db = _FakeDB([log], [story])
    evidence = durable_certification_evidence(db)
    assert 107 in evidence
    assert evidence[107]["db_story_id"] == 99
    assert evidence[107]["telegram_story_id"] == 7
    assert 108 not in evidence


def test_classify_stories_too_much_keeps_certified():
    from types import SimpleNamespace
    from src.stories.fleet_certification import classify_account, SessionInspection

    account = SimpleNamespace(status="active", health_status="alive", last_active=None,
                              last_story_success_at=None, story_precheck_status="rate_limited")
    inspection = SessionInspection("string", None, True, True, None, [], None)
    probe = {
        "probe_status": "story_probe_failed",
        "auth_valid": True,
        "identity_matches": True,
        "story_api_available": False,
        "story_probe_status": "rate_limited",
        "story_probe_reason": "RPCError 400: STORIES_TOO_MUCH (caused by CanSendStoryRequest)",
    }
    classification, reasons = classify_account(
        account=account,
        enabled=True,
        config_complete=True,
        inspection=inspection,
        probe=probe,
        certified=True,
    )
    assert classification == "CERTIFIED_PUBLISH"
    assert "capacity_stories_too_much" in reasons
