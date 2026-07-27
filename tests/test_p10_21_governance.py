"""P10.21 account governance refactor regressions."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.database import Base
from src.core.models import Account, AccountStatus
from src.governance.account_roles import add_role, get_account_roles, has_role, remove_role
from src.governance.constants import STORY_ALLOWED, SYSTEM_PROTECTED
from src.governance.fallback import fallback_roles_for_account
from src.governance.governance_resolver import build_execution_eligibility_preview, resolve_account_governance
from src.core.account_protection import PROTECTED_IDS

TOKEN = "p10-21-test-token"


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


def test_fallback_protected_roles_cannot_be_removed() -> None:
    protected_id = next(iter(PROTECTED_IDS))
    roles = fallback_roles_for_account(protected_id)
    assert SYSTEM_PROTECTED in roles
    result = remove_role(protected_id, SYSTEM_PROTECTED)
    assert result.get("removed") is False


def test_add_role_and_resolver_visibility() -> None:
    db = _memory_db()
    account = Account(id=140, phone_number="+10000000140", status=AccountStatus.ACTIVE, purpose="both")
    db.add(account)
    db.commit()

    add_role(140, STORY_ALLOWED, reason="test", created_by="pytest", db=db)
    db.commit()

    assert has_role(140, STORY_ALLOWED, db=db)
    gov = resolve_account_governance(db, account)
    assert gov["account_id"] == 140
    assert STORY_ALLOWED in gov["roles"]
    assert isinstance(gov["blocked_reasons"], list)


def test_protected_account_stays_protected_with_story_role() -> None:
    db = _memory_db()
    protected_id = next(iter(PROTECTED_IDS))
    account = Account(id=protected_id, phone_number="+10000000110", status=AccountStatus.ACTIVE, purpose="both")
    db.add(account)
    db.commit()

    add_role(protected_id, STORY_ALLOWED, reason="should not widen", db=db)
    db.commit()

    gov = resolve_account_governance(db, account)
    assert gov["protected"] is True
    assert gov["eligible_story_runtime"] is False
    assert SYSTEM_PROTECTED in gov["roles"]


def test_eligibility_preview_returns_json_shape() -> None:
    db = _memory_db()
    for aid in (140, 150):
        db.add(Account(id=aid, phone_number=f"+{aid}", status=AccountStatus.ACTIVE, purpose="both"))
    db.commit()
    preview = build_execution_eligibility_preview(db, module="stories")
    assert preview["visibility_only"] is True
    assert preview["runtime_execution_unchanged"] is True
    assert "eligible" in preview["summary"]


def test_governance_api_returns_json_not_html(monkeypatch) -> None:
    app = _app(monkeypatch)
    resp = app.test_client().get("/api/governance/eligibility-preview?module=stories", headers=_headers())
    assert resp.status_code == 200
    assert resp.is_json
    assert "<!doctype" not in resp.get_data(as_text=True).lower()


def test_roles_api_requires_reason_for_quarantined(monkeypatch) -> None:
    app = _app(monkeypatch)
    resp = app.test_client().post(
        "/api/accounts/140/roles",
        json={"role": "QUARANTINED"},
        headers=_headers(),
    )
    assert resp.status_code == 400
    assert resp.is_json


def test_accounts_main_template_has_governance_badges() -> None:
    text = open("src/dashboard/templates/accounts_main.html", encoding="utf-8").read()
    # Accounts dashboard simplify: operator presentation replaces inline governance badge chips.
    assert "operator_summary" in text
    assert "openGovernanceModal" in text
    assert "governanceModal" in text
    assert "Campaign governance" in text


def test_accounts_main_page_injects_eligibility_preview_fallback(monkeypatch) -> None:
    from src.dashboard import accounts_legacy_redirect as legacy

    monkeypatch.setattr(
        legacy,
        "build_accounts_main_context",
        lambda db: {
            "accounts": [],
            "total_accounts": 0,
            "operational_count": 0,
            "operator_summary": {
                "total_accounts": 0,
                "authorized": 0,
                "certified": 0,
                "ready": 0,
                "needs_session": 0,
                "disabled": 0,
                "protected": 0,
                "reserved": 0,
                "not_ready": 0,
            },
            "freshness_compact": {"label": "MISSING", "fresh": False, "text": "No matrix"},
            "matrix_freshness": {"present": False, "fresh": False, "label": "MISSING"},
            "eligibility_preview": {"summary": {"eligible": 0}},
            "eligibility_scheduler": {"summary": {"eligible": 0}},
            "eligibility_discovery": {"summary": {"eligible": 0}},
            "governance_roles_available": [],
            "campaign_execution_enabled": False,
            "pinned_ids": [],
        },
    )
    app = _app(monkeypatch)
    resp = app.test_client().get("/accounts", headers=_headers())
    assert resp.status_code == 200
    assert b"Accounts" in resp.data
    assert b"UndefinedError" not in resp.data
    assert b"openGovernanceModal" in resp.data or b"governanceModal" in resp.data


def test_ensure_governance_template_context_fills_missing_keys() -> None:
    from src.dashboard.accounts_legacy_redirect import _ensure_governance_template_context

    ctx = _ensure_governance_template_context({"accounts": [{"id": 1}]})
    assert "eligibility_preview" in ctx
    assert ctx["eligibility_preview"]["summary"]["eligible"] == 0
    assert ctx["accounts"][0]["governance_badges"] == []
