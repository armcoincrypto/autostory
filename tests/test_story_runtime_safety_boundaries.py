"""Clean runtime Story safety boundary tests (no Telegram or production DB)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.database import Base
from src.core.models import StoryRun, StoryRunStep, SystemLog


TOKEN = "story-runtime-safety-token"


def _minimal_app(monkeypatch):
    monkeypatch.setenv("DASHBOARD_ADMIN_TOKEN", TOKEN)
    monkeypatch.setenv("READINESS_WORKER_ENABLED", "false")
    monkeypatch.setenv("AI_AGENT_AUTO_LOOP_ENABLED", "false")
    import src.core.database as database
    import src.dashboard.app as app_module

    monkeypatch.setattr(database, "init_db", lambda: None)
    monkeypatch.setattr(app_module, "_load_optional_blueprints", lambda *_args: [])
    monkeypatch.setattr(app_module, "_import_dashboard_routes", lambda: (None, None))
    monkeypatch.setattr(app_module, "_ensure_dexpert_audit_route", lambda _app: None)
    app = app_module.create_app()
    app.config["TESTING"] = True
    return app


def test_story_routes_reject_unauthorized_before_database(monkeypatch) -> None:
    app = _minimal_app(monkeypatch)

    def _database_must_not_open():
        raise AssertionError("unauthorized Story request touched the database")

    monkeypatch.setattr(
        "src.dashboard.story_rotation_routes.get_db_context",
        _database_must_not_open,
    )
    client = app.test_client()
    requests = [
        ("post", "/api/stories/precheck"),
        ("post", "/api/stories/dry-run"),
        ("post", "/api/stories/runs"),
        ("get", "/api/stories/runtime-map"),
        ("get", "/api/stories/readiness-preview"),
        ("get", "/api/stories/eligible-accounts"),
        ("post", "/api/stories/publish"),
        ("get", "/api/stories/runs/1"),
    ]
    for method, path in requests:
        response = getattr(client, method)(path, json={} if method == "post" else None)
        assert response.status_code == 401
        assert response.get_json()["error"] == "unauthorized"


def test_authorized_story_route_uses_shared_dashboard_auth(monkeypatch) -> None:
    app = _minimal_app(monkeypatch)
    response = app.test_client().get(
        "/api/stories/runtime-map",
        headers={"X-Admin-Token": TOKEN},
    )
    assert response.status_code == 200
    assert response.get_json()["ok"] is True


def test_controlled_and_scheduler_story_gates_are_independent(monkeypatch) -> None:
    from src.stories.scheduler_integration import (
        controlled_story_execution_allowed,
        scheduler_story_execution_enabled,
    )

    monkeypatch.setenv("CONTROLLED_STORY_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("CONTROLLED_STORY_ACCOUNT_ID", "140")
    monkeypatch.setenv("SCHEDULER_STORY_EXECUTION_ENABLED", "false")
    assert controlled_story_execution_allowed(140) == (
        True,
        "controlled_story_publish_ok",
    )
    assert scheduler_story_execution_enabled() is False

    monkeypatch.setenv("CONTROLLED_STORY_EXECUTION_ENABLED", "false")
    monkeypatch.setenv("SCHEDULER_STORY_EXECUTION_ENABLED", "true")
    assert controlled_story_execution_allowed(140)[0] is False
    assert scheduler_story_execution_enabled() is True


def test_story_gates_default_deny_wrong_account_and_missing_purpose(monkeypatch) -> None:
    from src.core.execution_guard import ACTION_STORY_PUBLISH, can_execute_action
    from src.stories.scheduler_integration import controlled_story_execution_allowed

    monkeypatch.delenv("CONTROLLED_STORY_EXECUTION_ENABLED", raising=False)
    monkeypatch.delenv("CONTROLLED_STORY_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("SCHEDULER_STORY_EXECUTION_ENABLED", raising=False)
    assert controlled_story_execution_allowed(140)[0] is False

    monkeypatch.setenv("CONTROLLED_STORY_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("CONTROLLED_STORY_ACCOUNT_ID", "140")
    assert controlled_story_execution_allowed(141) == (
        False,
        "controlled_story_account_mismatch",
    )
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("STORY_MUTATIONS_ENABLED", "true")
    monkeypatch.setenv("STORY_EXECUTION_MODE", "controlled-canary")
    monkeypatch.setenv("STORY_ACCOUNT_MUTATION_ALLOWLIST", "140")
    monkeypatch.setenv("CONTROLLED_STORY_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("CONTROLLED_STORY_ACCOUNT_ID", "140")
    decision = can_execute_action(
        ACTION_STORY_PUBLISH,
        account_id=140,
        skip_audit=True,
    )
    assert decision.allowed is False
    assert decision.reason_code == "story_execution_purpose_required"


def test_legacy_story_flag_authorizes_neither_execution_purpose(monkeypatch) -> None:
    from src.stories.scheduler_integration import (
        controlled_story_execution_allowed,
        scheduler_story_execution_enabled,
    )

    monkeypatch.setenv("STORY_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("CONTROLLED_STORY_EXECUTION_ENABLED", "false")
    monkeypatch.setenv("SCHEDULER_STORY_EXECUTION_ENABLED", "false")
    monkeypatch.setenv("CONTROLLED_STORY_ACCOUNT_ID", "140")
    assert controlled_story_execution_allowed(140)[0] is False
    assert scheduler_story_execution_enabled() is False


def test_scheduler_tick_remains_hard_disabled_when_flag_true(monkeypatch) -> None:
    import asyncio

    from src.stories.scheduler_integration import maybe_tick_story_rotation

    monkeypatch.setenv("SCHEDULER_STORY_EXECUTION_ENABLED", "true")
    result = asyncio.run(maybe_tick_story_rotation())
    assert result["skipped"] is True
    # Fail-closed: mutations kill switch and/or certification gate keep tick inert.
    assert result["reason"] in {
        "scheduler_story_execution_not_certified",
        "story_mutations_disabled",
    }


class _FakeLock:
    def __init__(self) -> None:
        self.released = 0

    def release(self) -> None:
        self.released += 1


class _FakeClient:
    def __init__(self, *, connect_error: Exception | None = None) -> None:
        self.connected = False
        self.disconnects = 0
        self.connect_error = connect_error

    async def connect(self) -> None:
        self.connected = True
        if self.connect_error:
            raise self.connect_error

    async def is_user_authorized(self) -> bool:
        return True

    def is_connected(self) -> bool:
        return self.connected

    async def disconnect(self) -> None:
        self.disconnects += 1
        self.connected = False


class _AccountContext:
    def __init__(self, account) -> None:
        self.account = account

    def __enter__(self):
        account = self.account

        class _Query:
            def filter(self, *_args):
                return self

            def first(self):
                return account

        return SimpleNamespace(query=lambda *_args: _Query())

    def __exit__(self, *_args):
        return False


@pytest.mark.asyncio
async def test_controlled_client_disconnects_and_releases_lock(monkeypatch) -> None:
    import src.stories.client_lifecycle as lifecycle

    lock = _FakeLock()
    client = _FakeClient()
    account = SimpleNamespace(id=140, proxy_config=None, phone_number="+10000000140")
    monkeypatch.setattr(
        lifecycle,
        "acquire_session_lock",
        lambda *_args, **_kwargs: (True, lock, None),
    )
    monkeypatch.setattr(lifecycle, "get_db_context", lambda: _AccountContext(account))
    monkeypatch.setattr(
        lifecycle,
        "resolve_telethon_session",
        lambda _account: (object(), "file", None),
    )
    monkeypatch.setattr(lifecycle, "TelegramClient", lambda *_args, **_kwargs: client)

    lease, error = await lifecycle.open_controlled_story_client(140)
    assert error is None
    assert lease is not None
    cleanup = await lease.close()
    assert cleanup == {"ok": True, "errors": []}
    assert client.disconnects == 1
    assert lock.released == 1


@pytest.mark.asyncio
async def test_controlled_client_releases_lock_when_connect_raises(monkeypatch) -> None:
    import src.stories.client_lifecycle as lifecycle

    lock = _FakeLock()
    client = _FakeClient(connect_error=RuntimeError("offline"))
    account = SimpleNamespace(id=140, proxy_config=None, phone_number="+10000000140")
    monkeypatch.setattr(
        lifecycle,
        "acquire_session_lock",
        lambda *_args, **_kwargs: (True, lock, None),
    )
    monkeypatch.setattr(lifecycle, "get_db_context", lambda: _AccountContext(account))
    monkeypatch.setattr(
        lifecycle,
        "resolve_telethon_session",
        lambda _account: (object(), "file", None),
    )
    monkeypatch.setattr(lifecycle, "TelegramClient", lambda *_args, **_kwargs: client)

    lease, error = await lifecycle.open_controlled_story_client(140)
    assert lease is None
    assert error == "connect_failed"
    assert client.disconnects == 1
    assert lock.released == 1


@pytest.mark.asyncio
async def test_cleanup_releases_lock_when_disconnect_raises() -> None:
    from src.stories.client_lifecycle import ControlledStoryClientLease

    class _FailingWrapper:
        account = SimpleNamespace(id=140)

        async def disconnect(self):
            raise RuntimeError("disconnect failed")

    lock = _FakeLock()
    cleanup = await ControlledStoryClientLease(_FailingWrapper(), lock).close()
    assert cleanup["ok"] is False
    assert cleanup["errors"] == ["disconnect_failed:RuntimeError"]
    assert lock.released == 1


@pytest.mark.asyncio
async def test_controlled_execution_closes_client_when_publisher_raises(
    monkeypatch,
) -> None:
    """Multi-account pipeline: _execute_controlled_live_story_run creates a
    real StoryRun, runs _publish_one_account per account, then finalizes via
    _finalize_multi_run (replaced the old single-account _finalize_run).
    Uses a real in-memory DB rather than mocking finalize, since finalize now
    does real StoryRun/StoryRunStep/SystemLog persistence -- this is a
    stronger test than the original (less mocking, more real behavior)."""
    import src.core.execution_guard as guard
    import src.stories.client_lifecycle as lifecycle
    import src.stories.controlled_live_run as controlled
    import src.stories.publisher as publisher_module

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    class _Context:
        def __enter__(self):
            return db

        def __exit__(self, *_args):
            return False

    class _Lease:
        wrapper = SimpleNamespace()

        def __init__(self):
            self.closes = 0

        async def close(self):
            self.closes += 1
            return {"ok": True, "errors": []}

    async def _open(_account_id):
        return lease, None

    async def _publish(**_kwargs):
        raise RuntimeError("publisher failed")

    lease = _Lease()
    monkeypatch.setattr(controlled, "get_db_context", lambda: _Context())
    monkeypatch.setattr(guard, "require_execution_allowed", lambda *_a, **_k: None)
    monkeypatch.setattr(lifecycle, "open_controlled_story_client", _open)
    monkeypatch.setattr(publisher_module.story_publisher, "publish_story", _publish)

    result = await controlled._execute_controlled_live_story_run(
        {"account_ids": [140], "media_path": "/tmp/story.jpg", "mentions_per_story": 0},
        {"media": {"path": "/tmp/story.jpg"}},
    )
    assert result["ok"] is False
    assert lease.closes == 1
    step = result["steps"][0]
    assert step["error"] and "publisher failed" in step["error"]
    assert step["cleanup"]["ok"] is True


@pytest.mark.asyncio
async def test_post_telegram_persistence_failure_is_ambiguous_without_retry(
    monkeypatch, tmp_path
) -> None:
    import src.core.execution_guard as guard
    import src.stories.publisher as publisher_module
    from src.stories.publisher import StoryPublisher

    class _Update:
        story = SimpleNamespace(id=77)

    class _TelegramResult:
        updates = [_Update()]

    class _PublisherClient(_FakeClient):
        def __init__(self):
            super().__init__()
            self.send_calls = 0

        async def upload_file(self, _path):
            return object()

        async def __call__(self, _request):
            self.send_calls += 1
            return _TelegramResult()

    class _PersistenceFailure:
        def __enter__(self):
            raise RuntimeError("database unavailable")

        def __exit__(self, *_args):
            return False

    async def _no_pause(*_args, **_kwargs):
        return None

    async def _fake_invoke_send_story(client, request, *, authorization, account_id):
        return await client(request)

    client = _PublisherClient()
    account = SimpleNamespace(id=140)
    media = tmp_path / "story.jpg"
    media.write_bytes(b"fixture")
    monkeypatch.setattr(guard, "require_execution_allowed", lambda *_a, **_k: None)
    monkeypatch.setattr(publisher_module, "get_db_context", lambda: _PersistenceFailure())
    monkeypatch.setattr(publisher_module.AntiDetection, "random_pause", _no_pause)
    # Phase 1.1 mutation-boundary gate now runs inside publish_story before any
    # Telegram call; bypass it the same way test_story_mention_handoff.py does,
    # so this test still reaches the post-send persistence-failure path it
    # actually exercises.
    monkeypatch.setattr(
        "src.stories.mutation_boundary.require_story_mutation_authorization",
        lambda **_kwargs: SimpleNamespace(allowed=True, authorization=object(), denial_reason=None),
    )
    monkeypatch.setattr("src.stories.mutation_boundary.invoke_send_story", _fake_invoke_send_story)

    result = await StoryPublisher().publish_story(
        SimpleNamespace(client=client, account=account),
        str(media),
        execution_scope="controlled_live",
    )
    assert result["success"] is False
    assert result["ambiguous_no_retry"] is True
    assert result["result_classification"] == "AMBIGUOUS_NO_RETRY"
    assert result["story_id"] == 77
    assert client.send_calls == 1


def test_ambiguous_result_is_persisted_on_existing_run(monkeypatch) -> None:
    """_finalize_run was replaced by _finalize_multi_run (multi-account:
    account_ids + step_results, not a single account_id). Same safety
    property under test: a step whose publish_result carries
    ambiguous_no_retry=True (Telegram accepted the send, but local
    persistence then failed) must durably land as ambiguous_no_retry on the
    StoryRun, the StoryRunStep, and a SystemLog row -- never silently
    downgraded to an ordinary retryable failure."""
    import src.stories.controlled_live_run as controlled

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    run = StoryRun(mode="once", media_path="/tmp/story.jpg", status="running")
    db.add(run)
    db.commit()

    class _Context:
        def __enter__(self):
            return db

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(controlled, "get_db_context", lambda: _Context())
    step_results = [
        {
            "account_id": 140,
            "success": False,
            "error": "local_story_persistence_failed:RuntimeError: database unavailable",
            "publish_result": {
                "success": False,
                "ambiguous_no_retry": True,
                "telegram_accepted": True,
                "story_id": 77,
                "db_id": None,
                "error": "local_story_persistence_failed",
            },
            "mention_plan": [],
            "cleanup": {"ok": True, "errors": []},
        }
    ]
    result = controlled._finalize_multi_run(
        run_id=run.id,
        account_ids=[140],
        step_results=step_results,
        flat_mention_plan=[],
    )

    db.refresh(run)
    step = db.query(StoryRunStep).filter(StoryRunStep.run_id == run.id).one()
    log = db.query(SystemLog).filter(
        SystemLog.component == "controlled_live_story_run"
    ).one()
    assert result["result_classification"] == "AMBIGUOUS_NO_RETRY"
    assert run.status == "ambiguous_no_retry"
    assert step.status == "ambiguous_no_retry"
    assert step.account_id == 140
    assert step.story_id is None  # db_id, not the Telegram story_id -- never resent as if unpublished
    assert log.message == "controlled_story_multi_run_finished"
    assert log.level == "WARNING"
    assert log.details["status"] == "ambiguous_no_retry"

