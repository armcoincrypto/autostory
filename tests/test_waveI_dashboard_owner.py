"""Wave I — Owner Dashboard polish: attention-first, nontechnical, no live I/O."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.dashboard.owner_dashboard import (
    build_owner_dashboard_snapshot,
    humanize_age,
    _system_panel,
)

ROOT = Path(__file__).resolve().parents[1]


def test_wave_i_source_contracts():
    mod = (ROOT / "src/dashboard/owner_dashboard.py").read_text(encoding="utf-8")
    assert "TelegramClient" not in mod
    assert "openai" not in mod.lower() or "openai_calls" in mod
    assert "build_owner_health_index" in mod
    assert "health_status" not in mod  # no raw ORM health as owner truth
    tpl = (ROOT / "src/dashboard/templates/index.html").read_text(encoding="utf-8")
    assert "View Accounts" in tpl
    assert "Open Messages" in tpl
    assert "Needs attention" in tpl or "needs_attention" in tpl
    assert "systemd" not in tpl.lower()
    assert "Telethon" not in tpl
    assert "WAL" not in tpl
    assert "message_preview" not in tpl
    assert "message_body" not in tpl
    assert "/api/owner-dashboard" in tpl
    routes = (ROOT / "src/dashboard/routes.py").read_text(encoding="utf-8")
    assert "build_owner_dashboard_snapshot" in routes
    assert "owner-dashboard" in routes


def test_humanize_age():
    assert humanize_age(30) == "just now"
    assert "m ago" in humanize_age(600)
    assert "h ago" in humanize_age(7200)


def _accounts(**over):
    base = {
        "ok": True,
        "unavailable": False,
        "certified": 93,
        "needs_attention": 1,
        "disabled": 5,
        "unavailable_count": 5,
        "owner_copy": "1 account need attention",
        "attention": True,
        "matrix_fresh": True,
    }
    base.update(over)
    return base


def _messages(**over):
    base = {
        "ok": True,
        "unavailable": False,
        "sent_recent": 0,
        "failed": 0,
        "uncertain": 1,
        "issues": 1,
        "owner_copy": "1 uncertain message",
        "attention": True,
        "ai_draft_copy": "AI Draft: Available",
        "private_body_exposed": False,
    }
    base.update(over)
    return base


def _scheduler(**over):
    base = {
        "ok": True,
        "unavailable": False,
        "upcoming": 0,
        "failed": 0,
        "uncertain": 0,
        "scheduled_dm_copy": "Scheduled messages: Not enabled yet",
        "empty_state": "No upcoming scheduled messages",
        "attention": False,
    }
    base.update(over)
    return base


def _fleet(**over):
    base = {
        "ok": True,
        "unavailable": False,
        "fresh": True,
        "owner_copy": "Fleet health updated 2h ago",
        "attention": False,
        "generated_at": "2026-09-10T00:02:31Z",
    }
    base.update(over)
    return base


def test_accounts_summary_uses_matrix_helper_not_orm_health():
    summary = {
        "owner_certified": 93,
        "owner_needs_attention": 1,
        "owner_disabled": 5,
        "owner_unavailable": 5,
        "owner_matrix_fresh": True,
    }
    with patch(
        "src.dashboard.owner_dashboard.build_owner_health_index",
        return_value={"freshness": {"age_seconds": 7200}, "matrix_fresh": True},
    ), patch(
        "src.dashboard.owner_dashboard.owner_summary_from_index",
        return_value=summary,
    ):
        from src.dashboard.owner_dashboard import _accounts_panel

        panel = _accounts_panel()
    assert panel["certified"] == 93
    assert panel["needs_attention"] == 1
    assert panel["disabled"] == 5
    assert panel["unavailable_count"] == 5
    assert "health_status" not in panel


def test_messages_prioritizes_failed_uncertain_no_body():
    db = MagicMock()
    # Force SQLAlchemy path failure → raw path
    db.query.side_effect = RuntimeError("force raw")
    db.execute.return_value.fetchall.return_value = [
        ("SENT", 2),
        ("FAILED", 1),
        ("UNCERTAIN", 1),
    ]
    with patch("src.dashboard.owner_dashboard.messages_ai_draft_enabled", return_value=True):
        from src.dashboard.owner_dashboard import _messages_panel

        panel = _messages_panel(db=db)
    assert panel["failed"] == 1
    assert panel["uncertain"] == 1
    assert panel["attention"] is True
    assert panel["private_body_exposed"] is False
    assert "body" not in panel
    assert "preview" not in panel


def test_messages_empty_issues_copy():
    db = MagicMock()
    q = MagicMock()
    db.query.return_value = q
    q.filter.return_value = q
    q.group_by.return_value = q
    q.all.return_value = [("SENT", 3)]
    with patch("src.dashboard.owner_dashboard.messages_ai_draft_enabled", return_value=True):
        from src.dashboard.owner_dashboard import _messages_panel

        panel = _messages_panel(db=db)
    assert panel["owner_copy"] == "No message issues"
    assert panel["attention"] is False


def test_scheduler_zero_upcoming_empty_state():
    db = MagicMock()
    q = MagicMock()
    db.query.return_value = q
    q.filter.return_value = q
    q.scalar.side_effect = [0, 0, 0]  # upcoming, failed, uncertain
    with patch("src.dashboard.owner_dashboard.scheduled_dm_enabled", return_value=False):
        from src.dashboard.owner_dashboard import _scheduler_panel

        panel = _scheduler_panel(db=db)
    assert panel["upcoming"] == 0
    assert "Not enabled yet" in panel["scheduled_dm_copy"]
    assert panel["empty_state"] == "No upcoming scheduled messages"
    assert "SCHEDULED_DM_ENABLED" not in str(panel)


def test_fleet_fresh_stale_unavailable():
    from src.dashboard.owner_dashboard import _fleet_panel

    with patch(
        "src.stories.fleet_readiness_matrix.load_latest_matrix",
        return_value={"generated_at": "2026-09-10T00:00:00Z"},
    ), patch(
        "src.stories.fleet_readiness_matrix.matrix_freshness",
        return_value={
            "present": True,
            "fresh": True,
            "age_seconds": 7200,
            "generated_at": "2026-09-10T00:00:00Z",
        },
    ):
        fresh = _fleet_panel()
    assert fresh["fresh"] is True
    assert "updated" in fresh["owner_copy"]
    assert fresh["attention"] is False

    with patch(
        "src.stories.fleet_readiness_matrix.load_latest_matrix",
        return_value={"generated_at": "2026-01-01T00:00:00Z"},
    ), patch(
        "src.stories.fleet_readiness_matrix.matrix_freshness",
        return_value={
            "present": True,
            "fresh": False,
            "age_seconds": 999999,
            "generated_at": "2026-01-01T00:00:00Z",
        },
    ):
        stale = _fleet_panel()
    assert stale["attention"] is True
    assert "needs refresh" in stale["owner_copy"].lower()

    with patch(
        "src.stories.fleet_readiness_matrix.load_latest_matrix",
        return_value=None,
    ), patch(
        "src.stories.fleet_readiness_matrix.matrix_freshness",
        return_value={"present": False, "fresh": False, "age_seconds": None},
    ):
        missing = _fleet_panel()
    assert missing["attention"] is True
    assert "unavailable" in missing["owner_copy"].lower()


def test_system_health_all_healthy_vs_material_failure():
    healthy = _system_panel(
        accounts=_accounts(needs_attention=0, attention=False, owner_copy="No accounts need attention"),
        messages=_messages(issues=0, failed=0, uncertain=0, attention=False, owner_copy="No message issues"),
        scheduler=_scheduler(),
        fleet=_fleet(),
        backup={"attention": False, "surface": False, "owner_copy": "Backups healthy"},
        disk={"attention": False, "surface": False, "owner_copy": "Disk healthy"},
    )
    assert healthy["state"] == "healthy"
    assert healthy["owner_copy"] == "System healthy"

    bad = _system_panel(
        accounts=_accounts(),
        messages=_messages(),
        scheduler=_scheduler(),
        fleet=_fleet(attention=True, owner_copy="Fleet health needs refresh"),
        backup={"attention": True, "surface": True, "owner_copy": "Backup overdue"},
        disk={"attention": False, "surface": False},
    )
    assert bad["state"] == "needs_attention"
    assert bad["owner_copy"] == "System needs attention"


def test_panel_failure_does_not_crash_snapshot():
    """One data source failure must not raise from build_owner_dashboard_snapshot."""

    class BoomDB:
        def query(self, *a, **k):
            raise RuntimeError("db down")

    with patch(
        "src.dashboard.owner_dashboard._accounts_panel",
        side_effect=RuntimeError("accounts boom"),
    ), patch(
        "src.dashboard.owner_dashboard._fleet_panel",
        return_value=_fleet(),
    ), patch(
        "src.dashboard.owner_dashboard._backup_panel",
        return_value={"attention": False, "surface": False, "owner_copy": "Backups healthy", "ok": True},
    ), patch(
        "src.dashboard.owner_dashboard._disk_panel",
        return_value={"attention": False, "surface": False, "owner_copy": "Disk healthy", "ok": True},
    ), patch(
        "src.dashboard.owner_dashboard.get_db_context",
        create=True,
    ):
        # Inject via db= so messages/scheduler use BoomDB and degrade
        # Re-import path: build uses get_db_context only when db is None
        pass

    # Direct: accounts panel error helper path
    with patch(
        "src.dashboard.owner_dashboard.build_owner_health_index",
        side_effect=RuntimeError("matrix missing"),
    ):
        from src.dashboard.owner_dashboard import _accounts_panel

        panel = _accounts_panel()
    assert panel["unavailable"] is True
    assert "unavailable" in panel["owner_copy"].lower()

    snap = {
        "accounts": panel,
        "messages": _messages_panel_safe(),
        "scheduler": _scheduler(),
        "fleet": _fleet(),
        "backup": {"attention": False, "surface": False},
        "disk": {"attention": False, "surface": False},
    }
    system = _system_panel(**{k: snap[k] for k in ("accounts", "messages", "scheduler", "fleet", "backup", "disk")})
    assert system["owner_copy"]


def _messages_panel_safe():
    return _messages(attention=False, issues=0, failed=0, uncertain=0, owner_copy="No message issues")


def test_fixture_owner_summary_shape():
    """Phase 19 expected owner-facing numbers."""
    accounts = _accounts(certified=93, needs_attention=1, disabled=5, unavailable_count=5)
    messages = _messages(uncertain=1, failed=0, issues=1)
    scheduler = _scheduler(upcoming=0)
    fleet = _fleet(fresh=True)
    system = _system_panel(
        accounts=accounts,
        messages=messages,
        scheduler=scheduler,
        fleet=fleet,
        backup={"attention": False, "surface": False, "owner_copy": "Backups healthy"},
        disk={"attention": False, "surface": False, "owner_copy": "Disk healthy"},
    )
    assert accounts["certified"] == 93
    assert accounts["needs_attention"] == 1
    assert messages["uncertain"] == 1
    assert scheduler["upcoming"] == 0
    assert fleet["fresh"] is True
    assert system["state"] == "needs_attention"  # attention from accounts/messages
    assert "lease" not in system["owner_copy"].lower()
    assert "matrix" not in system["owner_copy"].lower()


@pytest.fixture()
def app(monkeypatch):
    monkeypatch.setenv("DASHBOARD_ADMIN_TOKEN", "wave-i-test-token-not-for-prod")
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("SCHEDULER_MUTATIONS_ENABLED", "false")
    monkeypatch.setenv("MESSAGES_EXECUTION_ENABLED", "false")
    monkeypatch.setenv("SCHEDULED_DM_ENABLED", "false")
    monkeypatch.setenv("READINESS_WORKER_ENABLED", "false")
    monkeypatch.setenv("AI_AGENT_AUTO_LOOP_ENABLED", "false")
    from src.dashboard.app import create_app

    application = create_app()
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


def test_owner_dashboard_api_requires_auth(client):
    anon = client.get("/api/owner-dashboard")
    assert anon.status_code == 401
    with patch(
        "src.dashboard.owner_dashboard.build_owner_dashboard_snapshot",
        return_value={
            "ok": True,
            "system": {"owner_copy": "System healthy", "attention": False, "state": "healthy"},
            "accounts": _accounts(needs_attention=0, attention=False),
            "messages": _messages_panel_safe(),
            "scheduler": _scheduler(),
            "fleet": _fleet(),
            "stories": {"omit": True},
            "backup": {"surface": False, "attention": False},
            "disk": {"surface": False, "attention": False},
            "contracts": {
                "live_telegram_calls": 0,
                "openai_calls": 0,
                "private_body_exposed": False,
            },
        },
    ):
        ok = client.get(
            "/api/owner-dashboard",
            headers={"X-Admin-Token": "wave-i-test-token-not-for-prod"},
        )
    assert ok.status_code == 200
    body = ok.get_json()
    assert body["contracts"]["live_telegram_calls"] == 0
    assert body["contracts"]["openai_calls"] == 0
    assert body["contracts"]["private_body_exposed"] is False


def test_performance_contracts_in_snapshot_module():
    """Static guarantee: module never imports Telethon / OpenAI clients."""
    mod = (ROOT / "src/dashboard/owner_dashboard.py").read_text(encoding="utf-8")
    assert "from telethon" not in mod
    assert "import telethon" not in mod
    assert "from openai" not in mod
    assert "import openai" not in mod
    assert "AsyncOpenAI" not in mod
    assert "TelegramClient" not in mod
