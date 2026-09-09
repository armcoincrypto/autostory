"""Wave D — Scheduler mutation scoping: DM on without PROMO/INFO."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_wave_d_source_contracts():
    flags = (ROOT / "src/messaging/scheduled_dm_flags.py").read_text(encoding="utf-8")
    assert "Independent of SCHEDULER_MUTATIONS_ENABLED" in flags or "independent of" in flags.lower()
    assert "return scheduled_dm_enabled()" in flags
    mut = (ROOT / "src/dashboard/scheduler_mutations.py").read_text(encoding="utf-8")
    assert "SCHEDULER_PROMO_MUTATIONS_ENABLED" in mut
    assert "SCHEDULER_INFO_MUTATIONS_ENABLED" in mut
    assert "job_type_scheduler_mutation_allowed" in mut
    routes = (ROOT / "src/dashboard/scheduler_routes.py").read_text(encoding="utf-8")
    assert "check_job_type_mutation_allowed" in routes


def test_dm_create_allowed_without_global_mutations(monkeypatch):
    monkeypatch.setenv("SCHEDULED_DM_ENABLED", "true")
    monkeypatch.setenv("SCHEDULER_MUTATIONS_ENABLED", "false")
    monkeypatch.setenv("SCHEDULER_PROMO_MUTATIONS_ENABLED", "false")
    monkeypatch.setenv("SCHEDULER_INFO_MUTATIONS_ENABLED", "false")
    from src.messaging.scheduled_dm_flags import scheduled_dm_create_allowed
    from src.dashboard.scheduler_mutations import (
        job_type_scheduler_mutation_allowed,
        scheduler_info_mutations_allowed,
        scheduler_promo_mutations_allowed,
    )

    assert scheduled_dm_create_allowed() is True
    assert job_type_scheduler_mutation_allowed("DM") is True
    assert scheduler_promo_mutations_allowed() is False
    assert scheduler_info_mutations_allowed() is False
    assert job_type_scheduler_mutation_allowed("PROMO") is False
    assert job_type_scheduler_mutation_allowed("INFO") is False


def test_promo_requires_global_and_type_flag(monkeypatch):
    from src.dashboard import scheduler_mutations as sm

    monkeypatch.setenv("SCHEDULER_MUTATIONS_ENABLED", "true")
    monkeypatch.setenv("SCHEDULER_PROMO_MUTATIONS_ENABLED", "false")
    monkeypatch.setenv("SCHEDULER_INFO_MUTATIONS_ENABLED", "false")
    # settings object may already be loaded — patch functions via env which _env_bool_flag reads
    monkeypatch.setattr(sm, "scheduler_mutations_enabled", lambda: True)
    assert sm.scheduler_promo_mutations_allowed() is False
    assert sm.job_type_scheduler_mutation_allowed("PROMO") is False

    monkeypatch.setenv("SCHEDULER_PROMO_MUTATIONS_ENABLED", "true")
    assert sm.scheduler_promo_mutations_allowed() is True
    assert sm.job_type_scheduler_mutation_allowed("PROMO") is True
    assert sm.job_type_scheduler_mutation_allowed("INFO") is False

    monkeypatch.setenv("SCHEDULER_INFO_MUTATIONS_ENABLED", "true")
    assert sm.scheduler_info_mutations_allowed() is True


def test_check_job_type_noop_when_global_off(monkeypatch):
    """Scoped pilots rely on before_request; type check must not double-block."""
    from src.dashboard import scheduler_mutations as sm

    monkeypatch.setattr(sm, "scheduler_mutations_enabled", lambda: False)
    assert sm.check_job_type_mutation_allowed("PROMO") is None


@pytest.fixture()
def app(monkeypatch):
    monkeypatch.setenv("DASHBOARD_ADMIN_TOKEN", "wave-d-test-token")
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("SCHEDULER_MUTATIONS_ENABLED", "true")
    monkeypatch.setenv("SCHEDULER_PROMO_MUTATIONS_ENABLED", "false")
    monkeypatch.setenv("SCHEDULER_INFO_MUTATIONS_ENABLED", "false")
    monkeypatch.setenv("SCHEDULED_DM_ENABLED", "true")
    monkeypatch.setenv("MESSAGES_EXECUTION_ENABLED", "false")
    monkeypatch.setenv("READINESS_WORKER_ENABLED", "false")
    monkeypatch.setenv("AI_AGENT_AUTO_LOOP_ENABLED", "false")

    # Force settings fields (pydantic may cache from process)
    from config.settings import settings

    monkeypatch.setattr(settings, "scheduler_mutations_enabled", True)
    monkeypatch.setattr(settings, "scheduler_promo_mutations_enabled", False)
    monkeypatch.setattr(settings, "scheduler_info_mutations_enabled", False)
    monkeypatch.setattr(settings, "scheduled_dm_enabled", True)

    from src.dashboard.app import create_app

    application = create_app()
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    return application


def test_run_now_promo_denied_when_type_flag_off(app):
    client = app.test_client()
    r = client.post(
        "/api/v1/jobs/run-now",
        json={"account_id": 1, "target_id": 1, "type": "PROMO"},
        headers={"X-Admin-Token": "wave-d-test-token"},
    )
    assert r.status_code == 423
    body = r.get_json() or {}
    assert body.get("code") == "scheduler_job_type_mutations_disabled"
    assert body.get("job_type") == "PROMO"


def test_run_now_info_denied_when_type_flag_off(app):
    client = app.test_client()
    r = client.post(
        "/api/v1/jobs/run-now",
        json={"account_id": 1, "target_id": 1, "type": "INFO"},
        headers={"X-Admin-Token": "wave-d-test-token"},
    )
    assert r.status_code == 423
    body = r.get_json() or {}
    assert body.get("job_type") == "INFO"
