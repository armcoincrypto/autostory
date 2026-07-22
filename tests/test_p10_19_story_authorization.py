"""P10.19 fresh Story authorization and one-account live gate regressions."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.database import Base
from src.core.models import Account, AccountStatus
from src.core.scheduler_models import AccountReadinessSnapshot
from src.stories import rotation_audit
from src.stories.rotation_audit import CONTROLLED_LIVE_ACCOUNT_ID, build_story_rotation_precheck

TOKEN = "p10-19-test-token"


def _write_valid_vertical_story_jpeg(path: Path, size: tuple[int, int] = (1080, 1920)) -> Path:
    """Deterministic Story-compatible RGB JPEG (decodable, vertical)."""
    Image.new("RGB", size, (40, 80, 120)).save(path, format="JPEG", quality=85, optimize=True)
    return path


def _app(monkeypatch, **env):
    monkeypatch.setenv("DASHBOARD_ADMIN_TOKEN", TOKEN)
    monkeypatch.setenv(
        "CONTROLLED_STORY_EXECUTION_ENABLED",
        env.get(
            "CONTROLLED_STORY_EXECUTION_ENABLED",
            env.get("STORY_EXECUTION_ENABLED", "false"),
        ),
    )
    monkeypatch.setenv("SCHEDULER_STORY_EXECUTION_ENABLED", "false")
    monkeypatch.setenv("CONTROLLED_STORY_ACCOUNT_ID", env.get("CONTROLLED_STORY_ACCOUNT_ID", ""))
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    return app


def _headers() -> dict[str, str]:
    return {"X-Admin-Token": TOKEN}


def _memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)()


def _payload(media_path: str = "data/media/Screenshot_2026-05-27_at_01.22.37.png") -> dict:
    return {
        "account_ids": [CONTROLLED_LIVE_ACCOUNT_ID],
        "media_path": media_path,
        "caption": "p10.19 test",
        "mentions_per_story": 0,
        "mode": "once",
    }


def test_account_140_fresh_auth_probe_dry_run_no_publish(monkeypatch, tmp_path, capsys) -> None:
    from scripts.ops import p10_19_story_authorization_probe as probe_script

    async def _fake_probe(account_id: int) -> dict:
        return {
            "account_id": account_id,
            "status": "allowed",
            "reason": "CanSendStory OK",
            "retry_after_seconds": None,
            "checked_at": "2026-05-27T00:00:00Z",
        }

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(probe_script, "_run_probe", _fake_probe)
    monkeypatch.setattr(
        probe_script,
        "_account_snapshot",
        lambda account_id: {
            "account_id": account_id,
            "found": True,
            "fresh_story_auth_ok": False,
        },
    )
    monkeypatch.setattr(sys, "argv", ["probe", "--dry-run", "--account", "140"])

    assert probe_script.main() == 0

    out = capsys.readouterr().out
    assert "p10_19_story_authorization_probe" in out
    report = next((tmp_path / "data/recovery_lab/reports").glob("*.json")).read_text()
    assert '"no_publish": true' in report
    assert '"applied": false' in report


def test_stale_legacy_health_not_hard_blocking_dry_run(monkeypatch, tmp_path) -> None:
    db = _memory_db()
    now = datetime.utcnow()
    media = _write_valid_vertical_story_jpeg(tmp_path / "story.jpg")
    account = Account(
        id=CONTROLLED_LIVE_ACCOUNT_ID,
        phone_number="+10000000140",
        status=AccountStatus.ACTIVE,
        purpose="both",
        health_status="error",
        story_precheck_status="not_authorized",
        story_precheck_checked_at=now - timedelta(hours=2),
        stories_today=0,
    )
    db.add(account)
    db.add(
        AccountReadinessSnapshot(
            account_id=CONTROLLED_LIVE_ACCOUNT_ID,
            status="READY",
            reason="v1 ready",
            checked_at=now,
            expires_at=now + timedelta(hours=1),
        )
    )
    db.commit()
    monkeypatch.setattr(rotation_audit, "account_has_usable_story_session", lambda account: True)
    monkeypatch.setattr(rotation_audit, "scheduler_telethon_excluded_account_ids", lambda db: set())
    monkeypatch.setattr(
        rotation_audit,
        "get_story_safety_decision",
        lambda account, requested_action: SimpleNamespace(
            allowed=False,
            reason_code="auth_required",
            human_reason="legacy health stale",
            next_allowed_at=None,
        ),
    )

    report = build_story_rotation_precheck(
        db,
        {
            "account_ids": [CONTROLLED_LIVE_ACCOUNT_ID],
            "media_path": str(media),
            "mentions_per_story": 0,
        },
    )

    account_row = report["accounts"][0]
    assert report["ok"] is True
    assert account_row["story_ready"] is True
    assert "health_not_alive:error" not in account_row["blockers"]
    assert "story_precheck_not_allowed:not_authorized" not in account_row["blockers"]
    assert account_row["live_only_blockers"] == ["fresh_story_auth_required"]


def test_live_only_blockers_compacted_in_ui() -> None:
    template = open("src/dashboard/templates/stories.html", encoding="utf-8").read()

    assert "Fresh story auth" in template or "fresh_story_auth" in template
    # Progressively disclosed diagnostics panel (label evolved from "Technical details").
    assert "Technical diagnostics" in template
    assert 'id="story-advanced-diagnostics"' in template
    assert "LIVE_STORY_ACCOUNT_140" in template


def test_live_publish_blocked_without_explicit_approval(monkeypatch) -> None:
    app = _app(monkeypatch, STORY_EXECUTION_ENABLED="true", CONTROLLED_STORY_ACCOUNT_ID="140")
    payload = _payload()
    payload.pop("explicit_operator_approval", None)
    payload.pop("confirmation_token", None)

    resp = app.test_client().post("/api/stories/runs", json=payload, headers=_headers())

    assert resp.status_code == 403
    assert resp.is_json
    payload = resp.get_json()
    assert payload["ok"] is False
    assert payload["error"] == "operator_approval_required"


def test_all_account_live_publish_blocked(monkeypatch) -> None:
    app = _app(monkeypatch, STORY_EXECUTION_ENABLED="true", CONTROLLED_STORY_ACCOUNT_ID="140")
    payload = {
        **_payload(),
        "account_ids": None,
        "explicit_operator_approval": True,
        "confirmation_token": "LIVE_STORY_ACCOUNT_140",
    }

    resp = app.test_client().post("/api/stories/runs", json=payload, headers=_headers())

    assert resp.status_code == 403
    assert resp.is_json
    assert resp.get_json()["error"] == "controlled_account_required"


def test_story_runs_api_returns_json_for_gate_blocks(monkeypatch) -> None:
    app = _app(monkeypatch)

    resp = app.test_client().post("/api/stories/runs", json={}, headers=_headers())

    assert resp.status_code == 403
    assert resp.is_json
    assert resp.get_json()["error"] == "controlled_story_execution_disabled"
    assert "<!doctype" not in resp.get_data(as_text=True).lower()
