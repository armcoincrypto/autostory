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
    monkeypatch.setenv(
        "CONTROLLED_STORY_EXECUTION_ENABLED",
        env.get(
            "CONTROLLED_STORY_EXECUTION_ENABLED",
            env.get("STORY_EXECUTION_ENABLED", "false"),
        ),
    )
    monkeypatch.setenv("SCHEDULER_STORY_EXECUTION_ENABLED", "false")
    monkeypatch.setenv(
        "CONTROLLED_STORY_ACCOUNT_ID",
        env.get("CONTROLLED_STORY_ACCOUNT_ID", ""),
    )
    # Phase 1.1: global kill switch. Enable only when a test explicitly exercises
    # deeper controlled-live gates (still cannot publish without mode/allowlist + token).
    monkeypatch.setenv(
        "STORY_MUTATIONS_ENABLED",
        env.get("STORY_MUTATIONS_ENABLED", "false"),
    )
    monkeypatch.setenv(
        "STORY_EXECUTION_MODE",
        env.get("STORY_EXECUTION_MODE", "disabled"),
    )
    monkeypatch.setenv(
        "STORY_ACCOUNT_MUTATION_ALLOWLIST",
        env.get("STORY_ACCOUNT_MUTATION_ALLOWLIST", ""),
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
    assert body["error"] == "story_mutations_disabled"


def test_runs_locked_when_controlled_flag_false_but_global_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app(monkeypatch, STORY_MUTATIONS_ENABLED="true")
    resp = app.test_client().post("/api/stories/runs", json=_payload(), headers=_headers())
    assert resp.status_code == 403
    assert resp.get_json()["error"] == "controlled_story_execution_disabled"


def test_runs_wrong_account(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app(
        monkeypatch,
        STORY_EXECUTION_ENABLED="true",
        CONTROLLED_STORY_ACCOUNT_ID="140",
        STORY_MUTATIONS_ENABLED="true",
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
        STORY_MUTATIONS_ENABLED="true",
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
        STORY_MUTATIONS_ENABLED="true",
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
        STORY_MUTATIONS_ENABLED="true",
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
    monkeypatch.setattr("src.stories.rotation_audit.account_has_usable_story_session", lambda account: True)
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
        STORY_MUTATIONS_ENABLED="true",
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
        STORY_MUTATIONS_ENABLED="true",
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


def test_controlled_live_account_follows_env_account_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """CONTROLLED_STORY_ACCOUNT_ID selects the single allowed controlled-live account."""
    from src.stories.rotation_audit import controlled_live_account_id
    from src.stories.controlled_live_run import live_confirmation_token

    monkeypatch.delenv("CONTROLLED_STORY_ACCOUNT_ID", raising=False)
    assert controlled_live_account_id() == 140
    assert live_confirmation_token() == "LIVE_STORY_ACCOUNT_140"

    monkeypatch.setenv("CONTROLLED_STORY_ACCOUNT_ID", "106")
    assert controlled_live_account_id() == 106
    assert live_confirmation_token() == "LIVE_STORY_ACCOUNT_106"
