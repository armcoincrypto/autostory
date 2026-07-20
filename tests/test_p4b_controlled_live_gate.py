"""P4B controlled live gate regressions (no live Telegram sends)."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.database import Base
from src.core.models import Account, AccountStatus
from src.core.scheduler_models import AccountReadinessSnapshot
from src.stories.rotation_audit import CONTROLLED_LIVE_ACCOUNT_ID

TOKEN = "p4b-test-token"


def _write_valid_vertical_story_jpeg(path: Path, size: tuple[int, int] = (1080, 1920)) -> Path:
    """Deterministic Story-compatible RGB JPEG (decodable, vertical)."""
    Image.new("RGB", size, (40, 80, 120)).save(path, format="JPEG", quality=85, optimize=True)
    return path


def _app(monkeypatch: pytest.MonkeyPatch, **env: str) -> object:
    monkeypatch.setenv("DASHBOARD_ADMIN_TOKEN", TOKEN)
    monkeypatch.setenv("STORY_EXECUTION_ENABLED", env.get("STORY_EXECUTION_ENABLED", "false"))
    monkeypatch.setenv(
        "CONTROLLED_STORY_ACCOUNT_ID",
        env.get("CONTROLLED_STORY_ACCOUNT_ID", ""),
    )
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    return app


def _headers() -> dict[str, str]:
    return {"X-Admin-Token": TOKEN}


def _payload(**overrides: object) -> dict:
    base = {
        "account_ids": [CONTROLLED_LIVE_ACCOUNT_ID],
        "media_path": "data/media/test.jpg",
        "caption": "p4b gate test",
        "mentions_per_story": 0,
        "mode": "once",
        "explicit_operator_approval": True,
        "confirmation_token": "LIVE_STORY_ACCOUNT_140",
    }
    base.update(overrides)
    return base


def test_runs_locked_when_execution_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app(monkeypatch)
    resp = app.test_client().post("/api/stories/runs", json=_payload(), headers=_headers())
    assert resp.status_code == 403
    body = resp.get_json()
    assert body["ok"] is False
    assert body["error"] == "story_execution_disabled"


def test_runs_wrong_account(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app(
        monkeypatch,
        STORY_EXECUTION_ENABLED="true",
        CONTROLLED_STORY_ACCOUNT_ID="140",
    )
    resp = app.test_client().post(
        "/api/stories/runs",
        json=_payload(account_ids=[106]),
        headers=_headers(),
    )
    assert resp.status_code == 403
    assert resp.get_json()["error"] == "controlled_account_required"


def test_runs_missing_operator_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app(
        monkeypatch,
        STORY_EXECUTION_ENABLED="true",
        CONTROLLED_STORY_ACCOUNT_ID="140",
    )
    resp = app.test_client().post(
        "/api/stories/runs",
        json=_payload(explicit_operator_approval=False),
        headers=_headers(),
    )
    assert resp.status_code == 403
    assert resp.get_json()["error"] == "operator_approval_required"


def test_runs_missing_confirmation_token(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app(
        monkeypatch,
        STORY_EXECUTION_ENABLED="true",
        CONTROLLED_STORY_ACCOUNT_ID="140",
    )
    resp = app.test_client().post(
        "/api/stories/runs",
        json=_payload(confirmation_token="WRONG"),
        headers=_headers(),
    )
    assert resp.status_code == 403
    assert resp.get_json()["error"] == "confirmation_token_required"


def test_runs_bad_media(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    app = _app(
        monkeypatch,
        STORY_EXECUTION_ENABLED="true",
        CONTROLLED_STORY_ACCOUNT_ID="140",
    )
    resp = app.test_client().post(
        "/api/stories/runs",
        json=_payload(media_path=str(tmp_path / "missing.jpg")),
        headers=_headers(),
    )
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "missing_or_invalid_media"


def test_runs_stale_fresh_auth(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    # Valid media is required so the gate reaches fresh-auth (media fails first with 400).
    media = _write_valid_vertical_story_jpeg(tmp_path / "story.jpg")

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db = Session()
    now = datetime.utcnow()
    account = Account(
        id=CONTROLLED_LIVE_ACCOUNT_ID,
        phone_number="+10000000140",
        status=AccountStatus.ACTIVE,
        purpose="both",
        health_status="error",
        story_precheck_status="allowed",
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

    class _Ctx:
        def __enter__(self) -> object:
            return db

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr("src.stories.controlled_live_run.get_db_context", lambda: _Ctx())
    monkeypatch.setattr("src.stories.rotation_audit.account_has_canonical_session", lambda account: True)
    monkeypatch.setattr("src.stories.rotation_audit.scheduler_telethon_excluded_account_ids", lambda db: set())
    monkeypatch.setattr(
        "src.stories.rotation_audit.get_story_safety_decision",
        lambda account, requested_action: SimpleNamespace(
            allowed=False,
            reason_code="auth_required",
            human_reason="legacy",
            next_allowed_at=None,
        ),
    )

    app = _app(
        monkeypatch,
        STORY_EXECUTION_ENABLED="true",
        CONTROLLED_STORY_ACCOUNT_ID="140",
    )
    resp = app.test_client().post(
        "/api/stories/runs",
        json=_payload(media_path=str(media)),
        headers=_headers(),
    )
    assert resp.status_code == 409
    assert resp.get_json()["error"] == "fresh_story_auth_required"


def test_publish_batch_still_locked(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app(
        monkeypatch,
        STORY_EXECUTION_ENABLED="true",
        CONTROLLED_STORY_ACCOUNT_ID="140",
    )
    client = app.test_client()
    publish = client.post(
        "/api/stories/publish",
        json={"account_id": 140, "media_path": "/tmp/x"},
        headers=_headers(),
    )
    batch = client.post(
        "/api/stories/batch",
        json={"account_ids": [140]},
        headers=_headers(),
    )
    assert publish.status_code == 403
    assert publish.get_json()["error"] == "story_execution_disabled"
    assert batch.status_code == 403
    assert batch.get_json()["error"] == "story_execution_disabled"


def test_dry_run_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app(monkeypatch)
    resp = app.test_client().post(
        "/api/stories/dry-run",
        json={"account_id": 140, "media_path": "/nonexistent"},
        headers=_headers(),
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body.get("dry_run") is True
