"""P10.22 story readiness truth unification regressions."""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.database import Base
from src.core.account_protection import PROTECTED_IDS
from src.core.models import Account, AccountStatus
from src.core.scheduler_models import AccountReadinessSnapshot
from src.stories import rotation_audit
from src.stories.rotation_audit import CONTROLLED_LIVE_ACCOUNT_ID
from src.stories.story_auth_state import resolve_story_auth_state
from src.stories.story_readiness_resolver import (
    build_story_readiness_preview,
    resolve_account_story_readiness,
)

TOKEN = "p10-22-test-token"


def _memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    import src.governance.models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)()


def _app(monkeypatch):
    monkeypatch.setenv("DASHBOARD_ADMIN_TOKEN", TOKEN)
    from src.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    return app


def _headers() -> dict[str, str]:
    return {"X-Admin-Token": TOKEN}


def _seed_accounts(db) -> None:
    now = datetime.utcnow()
    accounts = [
        Account(
            id=144,
            phone_number="+18096373691",
            status=AccountStatus.ACTIVE,
            purpose="both",
            health_status="alive",
            story_precheck_status=None,
            story_precheck_checked_at=None,
        ),
        Account(
            id=140,
            phone_number="+18096373640",
            status=AccountStatus.ACTIVE,
            purpose="both",
            health_status="alive",
            story_precheck_status="allowed",
            story_precheck_checked_at=now - timedelta(minutes=1),
        ),
    ]
    protected_id = next(iter(PROTECTED_IDS))
    accounts.append(
        Account(
            id=protected_id,
            phone_number="+19999999000",
            status=AccountStatus.ACTIVE,
            purpose="both",
            health_status="alive",
        )
    )
    db.add_all(accounts)
    for aid in (144, 140, protected_id):
        db.add(
            AccountReadinessSnapshot(
                account_id=aid,
                status="READY",
                checked_at=now,
                expires_at=now + timedelta(hours=1),
            )
        )
    db.commit()


def test_account_144_governance_ok_auth_required_not_runtime_ready() -> None:
    db = _memory_db()
    _seed_accounts(db)
    account = db.query(Account).filter(Account.id == 144).one()
    row = resolve_account_story_readiness(db, account)
    assert row["governance"]["allowed"] is True
    assert row["story_auth"]["state"] == "fresh_auth_required"
    assert row["runtime"]["final_story_ready"] is False
    assert row["labels"]["runtime"] == "Not ready"


def test_protected_account_governance_blocked_not_runtime_ready() -> None:
    db = _memory_db()
    _seed_accounts(db)
    protected_id = next(iter(PROTECTED_IDS))
    account = db.query(Account).filter(Account.id == protected_id).one()
    row = resolve_account_story_readiness(db, account)
    assert row["governance"]["allowed"] is False
    assert row["governance"]["protected"] is True
    assert row["runtime"]["final_story_ready"] is False


def test_readiness_preview_endpoint_json(monkeypatch) -> None:
    db = _memory_db()
    _seed_accounts(db)
    app = _app(monkeypatch)

    class _Ctx:
        def __enter__(self):
            return db

        def __exit__(self, *args):
            return False

    monkeypatch.setattr("src.dashboard.story_rotation_routes.get_db_context", lambda: _Ctx())
    resp = app.test_client().get("/api/stories/readiness-preview", headers=_headers())
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["visibility_only"] is True
    assert "summary" in data
    assert "accounts" in data
    assert data["summary"]["runtime_story_ready"] == 0


def test_missing_auth_cache_does_not_crash() -> None:
    db = _memory_db()
    account = Account(
        id=999,
        phone_number="+19999999999",
        status=AccountStatus.ACTIVE,
        purpose="both",
        story_precheck_status=None,
        story_precheck_checked_at=None,
    )
    db.add(account)
    db.commit()
    row = resolve_account_story_readiness(db, account)
    assert row["account_id"] == 999
    assert row["runtime"]["final_story_ready"] is False


def test_accounts_page_does_not_mark_auth_missing_as_story_ready(monkeypatch) -> None:
    db = _memory_db()
    _seed_accounts(db)
    app = _app(monkeypatch)

    class _Ctx:
        def __enter__(self):
            return db

        def __exit__(self, *args):
            return False

    monkeypatch.setattr("src.dashboard.accounts_legacy_redirect.get_db_context", lambda: _Ctx())
    body = app.test_client().get("/accounts", headers=_headers()).get_data(as_text=True)
    assert "Governance story-eligible:" in body
    assert "Runtime story-ready:" in body
    assert "Stories: Ready" not in body
    assert "Auth required" in body or "Not ready" in body


def test_accounts_page_renders_without_undefined_error(monkeypatch) -> None:
    db = _memory_db()
    _seed_accounts(db)
    app = _app(monkeypatch)

    class _Ctx:
        def __enter__(self):
            return db

        def __exit__(self, *args):
            return False

    monkeypatch.setattr("src.dashboard.accounts_legacy_redirect.get_db_context", lambda: _Ctx())
    resp = app.test_client().get("/accounts", headers=_headers())
    assert resp.status_code == 200
    assert "UndefinedError" not in resp.get_data(as_text=True)


def test_stories_template_structured_selected_account_card() -> None:
    text = open("src/dashboard/templates/stories.html", encoding="utf-8").read()
    assert "selected-account-readiness" in text
    assert "operatorStatusForRow" in text
    assert "Technical diagnostics" in text
    assert "refreshStoryReadinessState" in text
    assert "Step 1 — Choose destination" in text


def test_build_story_readiness_preview_summary_counts() -> None:
    db = _memory_db()
    _seed_accounts(db)
    preview = build_story_readiness_preview(db, include_protected=True)
    assert preview["summary"]["runtime_story_ready"] == 0
    assert preview["summary"]["governance_story_eligible"] >= 1


def _runtime_ready_account(db, *, account_id: int = CONTROLLED_LIVE_ACCOUNT_ID) -> Account:
    now = datetime.utcnow()
    account = Account(
        id=account_id,
        phone_number=f"+10000000{account_id}",
        status=AccountStatus.ACTIVE,
        purpose="both",
        health_status="alive",
        story_precheck_status="allowed",
        story_precheck_reason="CanSendStory OK",
        story_precheck_checked_at=now - timedelta(minutes=1),
        story_blocked_until=None,
        stories_today=0,
    )
    db.add(account)
    db.add(
        AccountReadinessSnapshot(
            account_id=account_id,
            status="READY",
            checked_at=now,
            expires_at=now + timedelta(hours=1),
        )
    )
    db.commit()
    return account


def test_account_with_persisted_allowed_auth_is_runtime_ready(monkeypatch) -> None:
    db = _memory_db()
    account = _runtime_ready_account(db)
    monkeypatch.setattr(rotation_audit, "account_has_canonical_session", lambda acc: True)
    monkeypatch.setattr(rotation_audit, "scheduler_telethon_excluded_account_ids", lambda db: set())
    monkeypatch.setattr(
        rotation_audit,
        "get_story_safety_decision",
        lambda account, requested_action: SimpleNamespace(
            allowed=True,
            reason_code="ok",
            human_reason="ok",
            next_allowed_at=None,
        ),
    )
    row = resolve_account_story_readiness(db, account)
    assert row["story_auth"]["state"] == "ok"
    assert row["story_auth"]["fresh"] is True
    assert row["needs_auth_probe"] is False
    assert row["runtime_story_ready"] is True
    assert "fresh_story_auth_required" not in row["runtime"]["blockers"]


def test_account_missing_auth_requires_probe() -> None:
    db = _memory_db()
    _seed_accounts(db)
    account = db.query(Account).filter(Account.id == 144).one()
    row = resolve_account_story_readiness(db, account)
    assert row["story_auth"]["state"] == "fresh_auth_required"
    assert row["needs_auth_probe"] is True
    assert row["runtime_story_ready"] is False


def test_account_allowed_auth_updates_summary_counts(monkeypatch) -> None:
    db = _memory_db()
    now = datetime.utcnow()
    db.add_all(
        [
            Account(
                id=201,
                phone_number="+10000000201",
                status=AccountStatus.ACTIVE,
                purpose="both",
                health_status="alive",
                story_precheck_status=None,
                story_precheck_checked_at=None,
            ),
            Account(
                id=CONTROLLED_LIVE_ACCOUNT_ID,
                phone_number="+10000000140",
                status=AccountStatus.ACTIVE,
                purpose="both",
                health_status="alive",
                story_precheck_status="allowed",
                story_precheck_reason="CanSendStory OK",
                story_precheck_checked_at=now - timedelta(minutes=1),
                story_blocked_until=None,
                stories_today=0,
            ),
        ]
    )
    for aid in (201, CONTROLLED_LIVE_ACCOUNT_ID):
        db.add(
            AccountReadinessSnapshot(
                account_id=aid,
                status="READY",
                checked_at=now,
                expires_at=now + timedelta(hours=1),
            )
        )
    db.commit()
    monkeypatch.setattr(rotation_audit, "account_has_canonical_session", lambda acc: True)
    monkeypatch.setattr(rotation_audit, "scheduler_telethon_excluded_account_ids", lambda db: set())
    monkeypatch.setattr(
        rotation_audit,
        "get_story_safety_decision",
        lambda account, requested_action: SimpleNamespace(
            allowed=True,
            reason_code="ok",
            human_reason="ok",
            next_allowed_at=None,
        ),
    )
    preview = build_story_readiness_preview(db, account_ids=[201, CONTROLLED_LIVE_ACCOUNT_ID])
    assert preview["summary"]["runtime_story_ready"] == 1
    assert preview["summary"]["needs_auth_probe"] == 1


def test_account_blocked_until_future_not_ready() -> None:
    db = _memory_db()
    now = datetime.utcnow()
    account = Account(
        id=202,
        phone_number="+10000000202",
        status=AccountStatus.ACTIVE,
        purpose="both",
        health_status="alive",
        story_precheck_status="allowed",
        story_precheck_checked_at=now - timedelta(minutes=1),
        story_blocked_until=now + timedelta(hours=2),
    )
    db.add(account)
    db.commit()
    auth = resolve_story_auth_state(account)
    assert auth["state"] == "blocked"
    assert auth["fresh"] is False
    row = resolve_account_story_readiness(db, account)
    assert row["runtime_story_ready"] is False
    assert auth["blockers"]


def test_stale_allowed_auth_maps_to_stale_not_required() -> None:
    db = _memory_db()
    now = datetime.utcnow()
    account = Account(
        id=203,
        phone_number="+10000000203",
        status=AccountStatus.ACTIVE,
        purpose="both",
        health_status="alive",
        story_precheck_status="allowed",
        story_precheck_checked_at=now - timedelta(hours=2),
        story_blocked_until=None,
    )
    db.add(account)
    db.commit()
    auth = resolve_story_auth_state(account)
    assert auth["state"] == "fresh_auth_stale"
    assert auth["blockers"] == ["fresh_story_auth_stale"]
    assert "fresh_story_auth_required" not in auth["blockers"]


def test_readiness_preview_endpoint_is_read_only(monkeypatch) -> None:
    db = _memory_db()
    _seed_accounts(db)
    app = _app(monkeypatch)

    class _Ctx:
        def __enter__(self):
            return db

        def __exit__(self, *args):
            return False

    monkeypatch.setattr("src.dashboard.story_rotation_routes.get_db_context", lambda: _Ctx())
    resp = app.test_client().get("/api/stories/readiness-preview", headers=_headers())
    assert resp.status_code == 200
    assert resp.get_json().get("visibility_only") is True
